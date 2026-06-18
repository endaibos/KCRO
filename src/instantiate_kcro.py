#!/usr/bin/env python3
"""
instantiate_kcro.py  —  build the KCRO ABox (knowledge graph) from the KubeObjects corpus.
Aligned with KCRO v0.3.0.

WHAT IT DOES
    Reads the per-object corpus, applies the KCRO TBox, and emits one RDF individual
    per Kubernetes object, with:
      * security findings as intrinsic aspects that  gufo:inheresIn  their bearer
      * inter-resource references resolved into  gufo:Relator  individuals that mediate
        the two objects involved.
    Output (kcro-abox.ttl) is loaded ALONGSIDE kcro.ttl and queried with SPARQL.

v0.3.0 ALIGNMENT
    * Bearer choices below mirror the twenty  inheresIn some <Bearer>  restrictions
      added in v0.3.0 one-to-one (container aspects -> Container, pod aspects -> Pod,
      RBAC aspects -> Role/ClusterRole, ClusterAdminBinding -> the binding relator,
      exposure/image-tag qualities -> Service/Container), so HermiT over TBox+ABox
      can check the instance data.
    * ClusterAdminBinding matches roleRef.name == "cluster-admin" ONLY, matching the
      ontology's datasetKey and its CIS 5.1.1 grounding. system:masters is a *group
      subject* (subjects[].kind == Group), not a roleRef, and is not modelled in
      v0.3.0 — if survey.py counted it, reconcile there.
    * EnvReference relators (containers[].envFrom[].configMapRef / .secretRef) are
      now materialised; they were missing from the 0.2.0 script.
    * PVC volume references are no longer recorded: KCRO has no PersistentVolumeClaim
      class (deferred to the stretch scope), so they could never resolve.
    * Digest-pinned images (name@sha256:...) are recognised explicitly as the safe
      case: neither ImageTagLatest nor ImageTagMissing.
    * --verify now counts distinct individuals (the graph deduplicates re-minted
      IRIs), reports relators mediating fewer than two entities (unresolved corpus
      references; gUFO expects >= 2), and one wildcard aspect is minted per
      offending RBAC *rule*, so wildcard counts keep survey.py's occurrence
      semantics (e.g. 1,684 WildcardVerbs).

WHY NOT security_analysis.json
    That file holds only aggregate counts. A traversable graph needs the individual
    objects and their resolved relationships, so we read the per-object data.

CONSISTENCY
    The field checks below mirror survey.py --security one-to-one. Same booleans,
    different output (an individual instead of count += 1). Running with --verify
    prints per-class instance counts so you can diff them against security_analysis.json.

INPUT
    --jsonl FILE   one JSON object per line: {repo, kind, namespace, name, content}
                   (content = the raw YAML string of the manifest)
    --arrow DIR    a HuggingFace Arrow dataset dir (the k8s_dataset/ that survey.py loads)
                   *** confirm the column names match your schema (see load_arrow). ***

AI DISCLOSURE
    The Kubernetes-to-KCRO mapping decisions and gUFO modelling choices are the
    author's. Claude (Anthropic) assisted with implementing/refactoring the
    ABox-construction engine (aspect/relator emitters, two-pass reference
    resolution, deterministic IRI minting, the v0.4.0 provenance extension) and
    documentation. All AI-assisted code was reviewed by the author; the generated
    ABox was validated against the survey counts (--verify) and the KCRO TBox.
    See MAPPER.md for the full artefact description.
"""

import argparse, hashlib, json, sys
from pathlib import Path
import yaml
from rdflib import Graph, Namespace, URIRef, Literal, RDF, RDFS, OWL

ROOT = Path(__file__).resolve().parent.parent   # repo root (src/ is one level down)

# ---------------------------------------------------------------- namespaces
KCRO = Namespace("https://w3id.org/kcro#")          # the TBox vocabulary
GUFO = Namespace("http://purl.org/nemo/gufo#")
DATA = Namespace("https://w3id.org/kcro/data#")     # the ABox (instances)
PROV = Namespace("http://www.w3.org/ns/prov#")      # provenance (v0.4.0)
ABOX_IRI = URIRef("https://w3id.org/kcro/data")     # ontology header of the ABox file

# ---------------------------------------------------------------- kind -> KCRO class
KIND_CLASS = {
    "Pod": "Pod", "Deployment": "Deployment", "StatefulSet": "StatefulSet",
    "DaemonSet": "DaemonSet", "ReplicaSet": "ReplicaSet", "Job": "Job", "CronJob": "CronJob",
    "Service": "Service", "Ingress": "Ingress", "NetworkPolicy": "NetworkPolicy",
    "ServiceAccount": "ServiceAccount", "Role": "Role", "ClusterRole": "ClusterRole",
    "RoleBinding": "RoleBinding", "ClusterRoleBinding": "ClusterRoleBinding",
    "ConfigMap": "ConfigMap", "Secret": "Secret",
}
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob"}

# ---------------------------------------------------------------- IRI minting
def _h(*parts):
    return hashlib.sha1("/".join(str(p) for p in parts).encode()).hexdigest()[:12]

def obj_iri(repo, kind, ns, name):
    return DATA[f"{KIND_CLASS.get(kind, 'Resource')}-{_h(repo, kind, ns, name)}"]

def child_iri(parent, suffix, *extra):
    return DATA[f"{suffix}-{_h(parent, suffix, *extra)}"]

def repo_iri(repo):
    return DATA[f"Repository-{_h(repo)}"]


class KCROGraph:
    def __init__(self):
        self.g = Graph()
        self.g.bind("kcro", KCRO); self.g.bind("gufo", GUFO); self.g.bind("", DATA)
        # ABox ontology header: lets Protege pull in the TBox (and gUFO transitively)
        self.g.add((ABOX_IRI, RDF.type, OWL.Ontology))
        self.g.add((ABOX_IRI, OWL.imports, URIRef("https://w3id.org/kcro")))
        # indexes for second-pass reference resolution, scoped per (repo, namespace)
        self.by_name = {}                 # (repo, ns, kind, name) -> IRI
        self.pods_by_label = {}           # (repo, ns) -> [(labels_dict, pod_iri)]
        self.deferred = []                # references to resolve in pass 2
        self.counts = {}                  # KCRO class -> n distinct individuals (--verify)
        self.repos_seen = set()           # repo names already minted as Repository

    # -- helpers -----------------------------------------------------------
    def add_type(self, indiv, cls):
        """Type an individual; count it once even if the same IRI is re-minted."""
        if (indiv, RDF.type, KCRO[cls]) not in self.g:
            self.counts[cls] = self.counts.get(cls, 0) + 1
        self.g.add((indiv, RDF.type, KCRO[cls]))

    def repo(self, repo_name):
        """Mint (once) the Repository individual for a source repo and return it.
        Metadata (stars/forks/language) is attached later via add_repo_meta()."""
        r = repo_iri(repo_name)
        if repo_name not in self.repos_seen:
            self.repos_seen.add(repo_name)
            self.add_type(r, "Repository")
            self.g.add((r, RDFS.label, Literal(repo_name)))
        return r

    def provenance(self, indiv, repo, ns=None):
        """Link an object/container to its source repo (PROV) and, optionally, ns."""
        self.g.add((indiv, PROV.wasDerivedFrom, self.repo(repo)))
        if ns is not None:
            self.g.add((indiv, KCRO.namespace, Literal(ns)))

    def aspect(self, bearer, cls, key, salt=()):
        """Mint an intrinsic aspect of class `cls` inhering in `bearer`.
        `salt` distinguishes multiple occurrences on one bearer (e.g. per RBAC rule)."""
        a = child_iri(bearer, cls, key, *salt)
        self.add_type(a, cls)
        self.g.add((a, GUFO.inheresIn, bearer))
        self.g.add((a, KCRO.datasetKey, Literal(key)))
        return a

    def relator(self, cls, *mediated, key=""):
        r = child_iri(cls, cls, key, *mediated)
        self.add_type(r, cls)
        for m in mediated:
            self.g.add((r, GUFO.mediates, m))
        return r

    # -- pass 1: objects + inhering aspects --------------------------------
    def ingest(self, repo, kind, ns, name, doc):
        if kind not in KIND_CLASS:
            return                                    # long-tail CRD: skip (open category)
        ns = ns or "default"
        o = obj_iri(repo, kind, ns, name)
        self.add_type(o, KIND_CLASS[kind])
        self.g.add((o, RDFS.label, Literal(f"{kind}/{name}", lang="en")))
        self.provenance(o, repo, ns)              # source repo + namespace
        self.by_name[(repo, ns, kind, name)] = o

        spec = (doc or {}).get("spec", {}) or {}

        if kind in WORKLOAD_KINDS or kind == "Pod":
            self._workload(repo, ns, o, kind, doc, spec)
        elif kind == "Service":
            self._service(o, spec); self._defer_service(repo, ns, o, spec)
        elif kind == "Ingress":
            self._ingress(o, spec); self._defer_ingress(repo, ns, o, spec)
        elif kind in ("Role", "ClusterRole"):
            self._rbac(o, doc)
        elif kind in ("RoleBinding", "ClusterRoleBinding"):
            self._binding(repo, ns, o, kind, doc)
        elif kind == "NetworkPolicy":
            self._defer_netpol(repo, ns, o, spec)

    # ---- workloads -------------------------------------------------------
    def _pod_spec(self, kind, spec):
        if kind == "Pod":
            return spec
        return ((spec.get("template") or {}).get("spec")) or {}   # workload template

    def _workload(self, repo, ns, owner, kind, doc, spec):
        podspec = self._pod_spec(kind, spec)
        # a workload manages a pod (template); the pod bears the pod-level aspects
        # (matching the v0.3.0 restrictions: pod-level aspects inhere in a kcro:Pod)
        if kind == "Pod":
            pod = owner
        else:
            pod = child_iri(owner, "Pod", "template")
            self.add_type(pod, "Pod")
            self.deferred.append(("manage", owner, pod))          # WorkloadManagement relator

        # pod-level aspects
        if podspec.get("hostNetwork") is True:
            self.aspect(pod, "HostNetworkPod", "pod.hostNetwork=true")
        if podspec.get("hostPID") is True:
            self.aspect(pod, "HostPIDPod", "pod.hostPID=true")
        if podspec.get("hostIPC") is True:
            self.aspect(pod, "HostIPCPod", "pod.hostIPC=true")
        if "automountServiceAccountToken" not in podspec:
            self.aspect(pod, "AutomountTokenEnabled", "pod.automountServiceAccountToken=missing")
        sa = podspec.get("serviceAccountName") or podspec.get("serviceAccount")
        if not sa or sa == "default":
            self.aspect(pod, "DefaultServiceAccount", "pod.serviceAccount=default")
        else:
            self.deferred.append(("identity", pod, repo, ns, sa))  # IdentityAssignment relator
        psc = podspec.get("securityContext")
        if not isinstance(psc, dict):
            psc = {}
        if "runAsNonRoot" not in psc:
            self.aspect(pod, "AbsentRunAsNonRoot", "pod.securityContext.runAsNonRoot=missing")
        elif psc.get("runAsNonRoot") is False:
            self.aspect(pod, "RunAsNonRootDisabled", "pod.securityContext.runAsNonRoot=false")
        if psc.get("runAsUser") == 0:
            self.aspect(pod, "PodRunAsRoot", "pod.securityContext.runAsUser=0")

        # volumes -> VolumeMount relators (Secret / ConfigMap).
        # PVC references are NOT recorded: KCRO v0.3.0 has no PersistentVolumeClaim
        # class (stretch scope), so they could never resolve to an individual.
        for v in podspec.get("volumes", []) or []:
            if "secret" in v:    self.deferred.append(("volume", pod, repo, ns, "Secret", (v["secret"] or {}).get("secretName")))
            if "configMap" in v: self.deferred.append(("volume", pod, repo, ns, "ConfigMap", (v["configMap"] or {}).get("name")))

        # containers -> container-level aspects + image and env references
        for c in podspec.get("containers", []) or []:
            cont = child_iri(pod, "Container", c.get("name", "c"))
            self.add_type(cont, "Container")
            self.g.add((cont, GUFO.isProperPartOf, pod))
            self.provenance(cont, repo)           # containers carry provenance too (CQ12)
            csc = c.get("securityContext")
            if not isinstance(csc, dict):
                csc = {}
            if csc.get("privileged") is True:
                self.aspect(cont, "PrivilegedContainer", "container.securityContext.privileged=true")
            if csc.get("allowPrivilegeEscalation") is True:
                self.aspect(cont, "PrivilegeEscalationAllowed", "container.securityContext.allowPrivilegeEscalation=true")
            if csc.get("runAsUser") == 0:
                self.aspect(cont, "ContainerRunAsRoot", "container.securityContext.runAsUser=0")
            if ((csc.get("capabilities") or {}).get("add")):
                self.aspect(cont, "AddedLinuxCapabilities", "container.capabilities.add=set")
            if not ((c.get("resources") or {}).get("limits")):
                self.aspect(cont, "AbsentResourceLimit", "container.resources.limits=missing")
            self._image(repo, ns, cont, c.get("image", ""))
            # envFrom -> EnvReference relators (new in the v0.3.0 script; mirrors
            # the corpus edges containers[].envFrom[].configMapRef / .secretRef)
            for src in (c.get("envFrom") or []):
                if not isinstance(src, dict):
                    continue
                if "configMapRef" in src:
                    self.deferred.append(("env", pod, repo, ns, "ConfigMap", (src["configMapRef"] or {}).get("name")))
                if "secretRef" in src:
                    self.deferred.append(("env", pod, repo, ns, "Secret", (src["secretRef"] or {}).get("name")))

    def _image(self, repo, ns, cont, image):
        if not image:
            return
        img_iri = DATA[f"ContainerImage-{_h(image)}"]
        self.add_type(img_iri, "ContainerImage")
        self.g.add((img_iri, RDFS.label, Literal(image)))
        # digest-pinned (name@sha256:...) is the safe case: neither latest nor missing
        if "@" not in image:
            tag = image.rsplit(":", 1)[1] if (":" in image.rsplit("/", 1)[-1]) else None
            if tag is None:
                self.aspect(cont, "ImageTagMissing", "container.image.tag=missing")
            elif tag == "latest":
                self.aspect(cont, "ImageTagLatest", "container.image.tag=latest")
        self.relator("ImageReference", cont, img_iri, key=image)

    # ---- service / ingress ----------------------------------------------
    def _service(self, o, spec):
        t = spec.get("type", "ClusterIP")
        cls = {"ClusterIP": "ClusterIPExposure", "NodePort": "NodePortExposure",
               "LoadBalancer": "LoadBalancerExposure", "ExternalName": "ExternalNameExposure"}.get(t)
        if cls:
            self.aspect(o, cls, f"service.type={t}")

    def _ingress(self, o, spec):
        if not spec.get("tls"):
            self.aspect(o, "AbsentIngressTLS", "ingress.tls=missing")

    # ---- rbac ------------------------------------------------------------
    def _rbac(self, o, doc):
        # one aspect per offending *rule* (salt=i), so --verify matches the
        # occurrence semantics of survey.py (e.g. 1,684 WildcardVerbs occurrences)
        for i, rule in enumerate((doc or {}).get("rules", []) or []):
            if "*" in (rule.get("verbs") or []):     self.aspect(o, "WildcardVerbs", "rbac.rule.verbs=wildcard", salt=(i,))
            if "*" in (rule.get("resources") or []): self.aspect(o, "WildcardResources", "rbac.rule.resources=wildcard", salt=(i,))
            if "*" in (rule.get("apiGroups") or []): self.aspect(o, "WildcardAPIGroups", "rbac.rule.apiGroups=wildcard", salt=(i,))

    def _binding(self, repo, ns, o, kind, doc):
        roleRef = (doc or {}).get("roleRef") or {}
        # cluster-admin only: matches the ontology's datasetKey and CIS 5.1.1.
        # system:masters is a Group *subject*, never a roleRef, and is out of scope
        # in v0.3.0 (see thesis note); reconcile with survey.py if it counted it.
        if roleRef.get("name") == "cluster-admin":
            self.aspect(o, "ClusterAdminBinding", "binding.roleRef.name=cluster-admin")
        # the binding individual is itself a relator mediating its subjects and its role
        rk = roleRef.get("kind"); rn = roleRef.get("name")
        if rk and rn:
            self.deferred.append(("bindrole", o, repo, ns, rk, rn))
        for subj in (doc or {}).get("subjects", []) or []:
            if subj.get("kind") == "ServiceAccount":
                self.deferred.append(("bindsubj", o, repo, subj.get("namespace") or ns, subj.get("name")))

    # ---- deferred-reference recorders -----------------------------------
    def _defer_service(self, repo, ns, o, spec):
        sel = spec.get("selector") or {}
        if sel: self.deferred.append(("route", o, repo, ns, sel))

    def _defer_ingress(self, repo, ns, o, spec):
        for rule in spec.get("rules", []) or []:
            for path in ((rule.get("http") or {}).get("paths") or []):
                svc = ((path.get("backend") or {}).get("service") or {}).get("name")
                if svc: self.deferred.append(("ingress", o, repo, ns, svc))

    def _defer_netpol(self, repo, ns, o, spec):
        sel = (spec.get("podSelector") or {}).get("matchLabels") or {}
        self.deferred.append(("netpol", o, repo, ns, sel))   # empty selector = whole namespace

    # -- index pod labels (called after pass 1) ----------------------------
    def index_pod_labels(self, repo, ns, kind, name, doc):
        if kind not in WORKLOAD_KINDS and kind != "Pod":
            return
        spec = (doc or {}).get("spec", {}) or {}
        labels = (doc.get("metadata") or {}).get("labels") if kind == "Pod" else \
                 ((spec.get("template") or {}).get("metadata") or {}).get("labels")
        if not labels:
            return
        pod = obj_iri(repo, kind, ns or "default", name) if kind == "Pod" \
              else child_iri(obj_iri(repo, kind, ns or "default", name), "Pod", "template")
        self.pods_by_label.setdefault((repo, ns or "default"), []).append((labels, pod))

    # -- pass 2: resolve references into relators --------------------------
    def resolve(self):
        for ref in self.deferred:
            tag = ref[0]
            if tag == "manage":
                _, owner, pod = ref
                self.relator("WorkloadManagement", owner, pod)
            elif tag == "identity":
                _, pod, repo, ns, saname = ref
                sa = self.by_name.get((repo, ns, "ServiceAccount", saname))
                if sa: self.relator("IdentityAssignment", pod, sa)
            elif tag in ("volume", "env"):
                _, pod, repo, ns, tkind, tname = ref
                tgt = self.by_name.get((repo, ns, tkind, tname))
                if tgt:
                    self.relator("VolumeMount" if tag == "volume" else "EnvReference", pod, tgt)
            elif tag == "bindrole":
                binding, repo, ns, rk, rn = ref[1], ref[2], ref[3], ref[4], ref[5]
                tgt = self.by_name.get((repo, ns, rk, rn)) or self.by_name.get((repo, "default", rk, rn))
                if tgt: self.g.add((binding, GUFO.mediates, tgt))
            elif tag == "bindsubj":
                binding, repo, ns, saname = ref[1], ref[2], ref[3], ref[4]
                sa = self.by_name.get((repo, ns, "ServiceAccount", saname))
                if sa: self.g.add((binding, GUFO.mediates, sa))
            elif tag in ("route", "netpol"):
                src, repo, ns, sel = ref[1], ref[2], ref[3], ref[4]
                cls = "ServiceRouting" if tag == "route" else "NetworkPolicyScope"
                for labels, pod in self.pods_by_label.get((repo, ns), []):
                    if sel and all(labels.get(k) == v for k, v in sel.items()):
                        self.relator(cls, src, pod, key=str(sorted(sel.items())))
            elif tag == "ingress":
                ing, repo, ns, svc = ref[1], ref[2], ref[3], ref[4]
                tgt = self.by_name.get((repo, ns, "Service", svc))
                if tgt: self.relator("IngressBackend", ing, tgt)

    # -- audit: relators mediating fewer than two entities ------------------
    def under_mediated(self):
        """gUFO expects a relator to mediate >= 2 entities. Bindings whose role or
        subjects were not found in the corpus end up below that; report them."""
        med = {}
        for r, _ in self.g.subject_objects(GUFO.mediates):
            med[r] = med.get(r, 0) + 1
        relator_classes = {KCRO[c] for c in (
            "RoleBinding", "ClusterRoleBinding", "WorkloadManagement", "IdentityAssignment",
            "VolumeMount", "EnvReference", "ImageReference", "ServiceRouting",
            "NetworkPolicyScope", "IngressBackend")}
        out = []
        for cls in relator_classes:
            for r in self.g.subjects(RDF.type, cls):
                if med.get(r, 0) < 2:
                    out.append(r)
        return out

    # -- attach GitHub metadata to the Repository individuals --------------
    def add_repo_meta(self, meta):
        """meta: {repo_name: {stars, forks, lang}}. Only repos already minted
        (i.e. that contributed an object) get annotated. Missing/NaN values are
        skipped (the GitHub enrichment isn't complete for every repo)."""
        def as_int(x):
            try:
                xf = float(x)
            except (TypeError, ValueError):
                return None
            return None if xf != xf else int(xf)        # NaN != NaN

        for name in self.repos_seen:
            m = meta.get(name)
            if not m:
                continue
            r = repo_iri(name)
            stars, forks = as_int(m.get("stars")), as_int(m.get("forks"))
            if stars is not None:
                self.g.add((r, KCRO.repoStars, Literal(stars)))
            if forks is not None:
                self.g.add((r, KCRO.repoForks, Literal(forks)))
            lang = m.get("lang")
            if isinstance(lang, str) and lang:
                self.g.add((r, KCRO.repoLanguage, Literal(lang)))


# ---------------------------------------------------------------- loaders
def load_jsonl(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            for doc in yaml.safe_load_all(row["content"]):
                if isinstance(doc, dict) and doc.get("kind"):
                    md = doc.get("metadata") or {}
                    yield row.get("repo", "repo"), doc["kind"], md.get("namespace"), md.get("name", "?"), doc

def load_arrow(path):
    from datasets import load_from_disk          # pip install datasets
    ds = load_from_disk(path)
    for row in ds:                                # *** confirm these column names ***
        for doc in yaml.safe_load_all(row["content"]):
            if isinstance(doc, dict) and doc.get("kind"):
                md = doc.get("metadata") or {}
                yield row.get("repo_full_name", "repo"), doc["kind"], md.get("namespace"), md.get("name", "?"), doc

def load_repo_meta(path):
    """One pass over the Arrow dataset for GitHub stats per repo (no YAML parse).
    Returns {repo_full_name: {stars, forks, lang}}."""
    from datasets import load_from_disk
    ds = load_from_disk(path)
    cols = [c for c in ("repo_full_name", "gh_stars", "gh_forks", "gh_language")
            if c in ds.column_names]
    meta = {}
    for row in ds.select_columns(cols):
        name = row.get("repo_full_name")
        if name and name not in meta:
            meta[name] = {"stars": row.get("gh_stars"), "forks": row.get("gh_forks"),
                          "lang": row.get("gh_language")}
    return meta


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl"); ap.add_argument("--arrow")
    ap.add_argument("--out", default=str(ROOT / "ontology" / "kcro-abox.ttl"))
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()

    rows = list(load_jsonl(a.jsonl) if a.jsonl else load_arrow(a.arrow))
    kg = KCROGraph()
    for repo, kind, ns, name, doc in rows:                 # pass 1
        kg.ingest(repo, kind, ns, name, doc)
    for repo, kind, ns, name, doc in rows:                 # index labels
        kg.index_pod_labels(repo, ns, kind, name, doc)
    kg.resolve()                                           # pass 2
    if a.arrow:                                            # GitHub stats per repo
        kg.add_repo_meta(load_repo_meta(a.arrow))

    kg.g.serialize(destination=a.out, format="turtle")
    print(f"wrote {a.out}: {len(kg.g)} triples")
    if a.verify:
        for cls in sorted(kg.counts):
            print(f"  {cls:28} {kg.counts[cls]}")
        um = kg.under_mediated()
        print(f"  relators mediating < 2 (unresolved corpus refs): {len(um)}")

if __name__ == "__main__":
    main()