#!/usr/bin/env python3
"""
instantiate_kcro.py  —  build the KCRO ABox (knowledge graph) from the KubeObjects corpus.

WHAT IT DOES
    Reads the per-object corpus, applies the KCRO TBox, and emits one RDF individual
    per Kubernetes object, with:
      * security findings as intrinsic aspects that  gufo:inheresIn  their bearer
      * inter-resource references resolved into  gufo:Relator  individuals that mediate
        the two objects involved.
    Output (kcro-abox.ttl) is loaded ALONGSIDE kcro.ttl and queried with SPARQL.

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
"""

import argparse, hashlib, json, sys
import yaml
from rdflib import Graph, Namespace, URIRef, Literal, RDF, RDFS, XSD

# ---------------------------------------------------------------- namespaces
KCRO = Namespace("https://w3id.org/kcro#")          # the TBox vocabulary
GUFO = Namespace("http://purl.org/nemo/gufo#")
DATA = Namespace("https://w3id.org/kcro/data#")     # the ABox (instances)

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


class KCROGraph:
    def __init__(self):
        self.g = Graph()
        self.g.bind("kcro", KCRO); self.g.bind("gufo", GUFO); self.g.bind("", DATA)
        # indexes for second-pass reference resolution, scoped per (repo, namespace)
        self.by_name = {}                 # (repo, ns, kind, name) -> IRI
        self.pods_by_label = {}           # (repo, ns) -> [(labels_dict, pod_iri)]
        self.deferred = []                # references to resolve in pass 2
        self.counts = {}                  # KCRO class -> n  (for --verify)

    # -- helpers -----------------------------------------------------------
    def add_type(self, indiv, cls):
        self.g.add((indiv, RDF.type, KCRO[cls]))
        self.counts[cls] = self.counts.get(cls, 0) + 1

    def aspect(self, bearer, cls, key):
        """Mint an intrinsic aspect of class `cls` inhering in `bearer`."""
        a = child_iri(bearer, cls, key)
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

        # volumes -> VolumeMount relators (Secret / ConfigMap / PVC)
        for v in podspec.get("volumes", []) or []:
            if "secret" in v:    self.deferred.append(("volume", pod, repo, ns, "Secret", (v["secret"] or {}).get("secretName")))
            if "configMap" in v: self.deferred.append(("volume", pod, repo, ns, "ConfigMap", (v["configMap"] or {}).get("name")))
            if "persistentVolumeClaim" in v:
                self.deferred.append(("volume", pod, repo, ns, "PersistentVolumeClaim", (v["persistentVolumeClaim"] or {}).get("claimName")))

        # containers -> container-level aspects + image references
        for c in podspec.get("containers", []) or []:
            cont = child_iri(pod, "Container", c.get("name", "c"))
            self.add_type(cont, "Container")
            self.g.add((cont, GUFO.isProperPartOf, pod))
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

    def _image(self, repo, ns, cont, image):
        if not image:
            return
        img_iri = DATA[f"ContainerImage-{_h(image)}"]
        self.add_type(img_iri, "ContainerImage")
        self.g.add((img_iri, RDFS.label, Literal(image)))
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
        for rule in (doc or {}).get("rules", []) or []:
            if "*" in (rule.get("verbs") or []):     self.aspect(o, "WildcardVerbs", "rbac.rule.verbs=wildcard")
            if "*" in (rule.get("resources") or []): self.aspect(o, "WildcardResources", "rbac.rule.resources=wildcard")
            if "*" in (rule.get("apiGroups") or []): self.aspect(o, "WildcardAPIGroups", "rbac.rule.apiGroups=wildcard")

    def _binding(self, repo, ns, o, kind, doc):
        roleRef = (doc or {}).get("roleRef") or {}
        if roleRef.get("name") in ("cluster-admin", "system:masters"):
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
            elif tag == "volume":
                _, pod, repo, ns, tkind, tname = ref
                tgt = self.by_name.get((repo, ns, tkind, tname))
                if tgt: self.relator("VolumeMount", pod, tgt)
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


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl"); ap.add_argument("--arrow")
    ap.add_argument("--out", default="kcro-abox.ttl")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()

    rows = list(load_jsonl(a.jsonl) if a.jsonl else load_arrow(a.arrow))
    kg = KCROGraph()
    for repo, kind, ns, name, doc in rows:                 # pass 1
        kg.ingest(repo, kind, ns, name, doc)
    for repo, kind, ns, name, doc in rows:                 # index labels
        kg.index_pod_labels(repo, ns, kind, name, doc)
    kg.resolve()                                           # pass 2

    kg.g.serialize(destination=a.out, format="turtle")
    print(f"wrote {a.out}: {len(kg.g)} triples")
    if a.verify:
        for cls in sorted(kg.counts):
            print(f"  {cls:28} {kg.counts[cls]}")

if __name__ == "__main__":
    main()
