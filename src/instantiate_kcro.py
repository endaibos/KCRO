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
    --jsonl FILE   one JSON object per line: {repo, content}, where repo may also be
                   given as repo_full_name (kind/namespace/name are read from the YAML)
                   (content = the raw YAML string of the manifest, may be multi-document)
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

# (apiGroup, kind) -> KCRO class, consulted BEFORE KIND_CLASS so a third-party kind
# that shares a name with a native one can never be mistaken for it. In the gitops
# corpus, "Application" is argoproj.io 133 / kots.io 31 / pipecd.dev 7 and "HelmChart"
# is kots.io 76 / helm.cattle.io 2 with no Flux HelmChart at all, so keying on kind
# alone would mis-type 38 Applications and 78 HelmCharts.
GROUP_KIND_CLASS = {
    ("apiextensions.k8s.io", "CustomResourceDefinition"): "CustomResourceDefinition",
    # --- the GitOps module (S3) ---------------------------------------------
    ("kustomize.toolkit.fluxcd.io", "Kustomization"):  "FluxKustomization",
    ("helm.toolkit.fluxcd.io",      "HelmRelease"):    "HelmRelease",
    ("source.toolkit.fluxcd.io",    "GitRepository"):  "GitRepository",
    ("source.toolkit.fluxcd.io",    "OCIRepository"):  "OCIRepository",
    ("source.toolkit.fluxcd.io",    "HelmRepository"): "HelmRepository",
    ("source.toolkit.fluxcd.io",    "HelmChart"):      "HelmChart",
    ("argoproj.io",                 "Application"):    "ArgoApplication",
    ("argoproj.io",                 "ApplicationSet"): "ArgoApplicationSet",
}

# Every class whose instances are custom resources: the open category and the eight
# typed Flux / Argo CD kinds. All of them are checked against their defining CRD.
CUSTOM_RESOURCE_CLASSES = ("CustomResource",) + tuple(
    c for c in GROUP_KIND_CLASS.values() if c != "CustomResourceDefinition")

# Flux source kinds a delivery resource can point at. Indexed per repository, so a
# reference can only resolve inside the repo that declares the source.
FLUX_SOURCE_CLASSES = ("GitRepository", "OCIRepository", "HelmRepository", "HelmChart")

# The API groups each of the 17 kinds legitimately comes from. A manifest carrying a
# native kind NAME from a foreign group is a different resource that happens to share
# the name: serving.knative.dev/Service, projectcalico.org/NetworkPolicy and
# talos.dev/ServiceAccount are not the native kinds (18 such objects in the gitops
# corpus, 2 in rahman).
#
# "extensions" stays allowed for the apps and networking kinds: extensions/v1beta1 is a
# genuine historical native version, and excluding it would wrongly drop 139 real
# workloads (44 in gitops, 95 in rahman).
EXPECTED_GROUPS = {
    "Pod": {""}, "Service": {""}, "ConfigMap": {""}, "Secret": {""},
    "ServiceAccount": {""},
    "Deployment": {"apps", "extensions"}, "StatefulSet": {"apps", "extensions"},
    "DaemonSet": {"apps", "extensions"}, "ReplicaSet": {"apps", "extensions"},
    "Job": {"batch"}, "CronJob": {"batch"},
    "Ingress": {"networking.k8s.io", "extensions"},
    "NetworkPolicy": {"networking.k8s.io", "extensions"},
    "Role": {"rbac.authorization.k8s.io"},
    "ClusterRole": {"rbac.authorization.k8s.io"},
    "RoleBinding": {"rbac.authorization.k8s.io"},
    "ClusterRoleBinding": {"rbac.authorization.k8s.io"},
}

# Native API groups. An object here is either in scope or DELIBERATELY descoped
# (PersistentVolumeClaim, PodDisruptionBudget, Namespace); it is never a custom
# resource. "" is the core group, i.e. apiVersion: v1.
NATIVE_GROUPS = frozenset({
    "", "apps", "batch", "extensions", "policy", "autoscaling",
    "rbac.authorization.k8s.io", "networking.k8s.io", "storage.k8s.io",
    "apiextensions.k8s.io", "admissionregistration.k8s.io", "apiregistration.k8s.io",
    "coordination.k8s.io", "scheduling.k8s.io", "node.k8s.io", "certificates.k8s.io",
    "authentication.k8s.io", "authorization.k8s.io", "events.k8s.io",
    "discovery.k8s.io", "flowcontrol.apiserver.k8s.io", "resource.k8s.io",
    "internal.apiserver.k8s.io"})

# Groups whose documents are read by a client-side tool and never submitted to an API
# server. Not cluster resources, so not custom resources either. 1,418 of the 1,426
# kustomize.config.k8s.io Kustomizations in the gitops corpus have no usable
# metadata.name, so they cannot even be individuated.
NON_CLUSTER_GROUPS = frozenset({
    "kustomize.config.k8s.io",            # kustomization.yaml / Component build files
    "kind.x-k8s.io",                      # kind cluster config
    "kuttl.dev", "chainsaw.kyverno.io",   # test-harness manifests
})

# S2 switch: type third-party kinds as kcro:CustomResource. Off until that stage, so
# each stage's coverage gain is measurable on its own.
ENABLE_OPEN_CATEGORY = True

# The relator classes under_mediated() checks. Hoisted verbatim from that method so
# reporting can count relator individuals without restating the list.
RELATOR_CLASSES = (
    "RoleBinding", "ClusterRoleBinding", "WorkloadManagement", "IdentityAssignment",
    "VolumeMount", "EnvReference", "ImageReference", "ServiceRouting",
    "NetworkPolicyScope", "IngressBackend", "SourceReference", "SchemaConformance")

# Relators added in v0.5.0. They are only minted when their target is found, so they
# can never be under-mediated; their quality is reported as the share of references
# that resolve, separately from the original relators.
NEW_RELATOR_CLASSES = ("SourceReference", "SchemaConformance")

# ---------------------------------------------------------------- IRI minting
def _h(*parts):
    return hashlib.sha1("/".join(str(p) for p in parts).encode()).hexdigest()[:12]

def obj_iri(repo, kind, ns, name):
    return DATA[f"{KIND_CLASS.get(kind, 'Resource')}-{_h(repo, kind, ns, name)}"]

def ext_obj_iri(cls, repo, group, kind, ns, name):
    """IRI for a group-keyed or open-category object. Includes the API group, so
    argoproj.io/Application and kots.io/Application cannot collide, and leaves
    obj_iri() byte-stable for the original 17 kinds."""
    return DATA[f"{cls}-{_h(repo, group, kind, ns, name)}"]

def api_group(doc):
    """The group part of apiVersion; "" for the core group. Never raises."""
    av = (doc or {}).get("apiVersion")
    return av.split("/", 1)[0] if isinstance(av, str) and "/" in av else ""

def kcro_class_for(kind, doc):
    """The single authority on whether KCRO models an object, and as what.
    Returns a KCRO class name, or None when the object is out of scope."""
    g = api_group(doc)
    cls = GROUP_KIND_CLASS.get((g, kind))
    if cls:
        return cls
    if kind in KIND_CLASS and g in EXPECTED_GROUPS.get(kind, {g}):
        return KIND_CLASS[kind]               # unchanged 17-kind behaviour
    if ENABLE_OPEN_CATEGORY and "." in g \
            and g not in NATIVE_GROUPS and g not in NON_CLUSTER_GROUPS:
        return "CustomResource"
    return None

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
        self.by_gk = {}                   # (repo, ns, group, kind, name) -> IRI, group-keyed
        self.crd_by_gk = {}               # (repo, group, kind) -> CRD IRI, for SchemaConformance
        self.flux_src = {}                # (repo, ns, kind, name) -> IRI, Flux sources
        self.unnameable = 0               # new-class objects skipped for want of a name
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
        cls = kcro_class_for(kind, doc)
        if cls is None:
            return                                    # out of scope: see kcro_class_for
        ns = ns or "default"
        group = api_group(doc)
        native17 = kind in KIND_CLASS and cls == KIND_CLASS[kind]
        generated = False
        if not native17:
            # Only the new classes get this treatment: the 17 keep their existing
            # behaviour so their IRIs stay byte-stable.
            md = (doc or {}).get("metadata")
            md = md if isinstance(md, dict) else {}
            if name in (None, "", "?"):
                gn = md.get("generateName")
                if isinstance(gn, str) and gn:
                    name, generated = gn, True       # rescues 312 of 326 Argo Workflows
                else:
                    # Skip and count rather than collapsing every unnamed object in a
                    # repository onto one "?" individual.
                    self.unnameable += 1
                    return
        if native17:
            o = obj_iri(repo, kind, ns, name)         # unchanged IRIs for the 17
            self.by_name[(repo, ns, kind, name)] = o
        else:
            o = ext_obj_iri(cls, repo, group, kind, ns, name)
            self.by_gk[(repo, ns, group, kind, name)] = o
        self.add_type(o, cls)
        self.g.add((o, RDFS.label, Literal(f"{kind}/{name}", lang="en")))
        self.provenance(o, repo, ns)              # source repo + namespace
        self.g.add((o, KCRO.resourceKind, Literal(kind)))
        self.g.add((o, KCRO.apiGroup, Literal(group)))
        av = (doc or {}).get("apiVersion")
        if isinstance(av, str) and av:
            self.g.add((o, KCRO.apiVersion, Literal(av)))

        spec = (doc or {}).get("spec", {}) or {}

        if generated:
            # an individual minted from generateName denotes a template, not a
            # cluster object: say so, so a reader can tell them apart
            self.g.add((o, KCRO.datasetKey, Literal("metadata.generateName")))
        if cls == "CustomResourceDefinition":
            self._crd(repo, o, spec)
        if cls in FLUX_SOURCE_CLASSES:
            self.flux_src[(repo, ns, kind, name)] = o
        if cls in ("FluxKustomization", "HelmRelease"):
            self._defer_source(repo, ns, o, spec)
        if cls in CUSTOM_RESOURCE_CLASSES:
            self.deferred.append(("crdtype", o, repo, group, kind))

        if not native17:
            # The rules below read the spec of the 17 native kinds. An object that only
            # shares a kind NAME with one of them (serving.knative.dev/Service,
            # batch.volcano.sh/Job) has a different schema and must not get them.
            return

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

    def _crd(self, repo, o, spec):
        """Record the type a CustomResourceDefinition declares and index it.

        The index stays REPO-SCOPED on purpose. A corpus-wide index would raise
        CRD linkage, but build_abox_partitioned.py is exact only because no edge
        crosses a repository boundary, so a global index would make the
        partitioned and unpartitioned builds disagree."""
        group = spec.get("group")
        names = spec.get("names") or {}
        declared = names.get("kind") if isinstance(names, dict) else None
        scope = spec.get("scope")
        if isinstance(group, str) and isinstance(declared, str):
            # spec.group/spec.names.kind are the type this CRD admits, which is NOT
            # apiextensions.k8s.io/CustomResourceDefinition: overwrite the generic
            # values written in ingest() so the join key is the declared type.
            self.g.set((o, KCRO.apiGroup, Literal(group)))
            self.g.set((o, KCRO.resourceKind, Literal(declared)))
            self.crd_by_gk[(repo, group, declared)] = o
        if isinstance(scope, str):
            self.g.add((o, KCRO.datasetKey, Literal(f"crd.scope={scope}")))

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
    def _defer_source(self, repo, ns, o, spec):
        """Flux names its source three different ways, so all three are read.

        Measured in the gitops corpus: spec.sourceRef 508 references,
        spec.chart.spec.sourceRef 117, spec.chartRef 405. Reading only the first
        would capture half the edges and report 18.9% resolution instead of 54.4%.
        A sourceRef with no namespace means the referring object's namespace.
        """
        refs = [spec.get("sourceRef"), spec.get("chartRef")]
        chart = spec.get("chart")
        if isinstance(chart, dict) and isinstance(chart.get("spec"), dict):
            refs.append(chart["spec"].get("sourceRef"))
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            kind, name = ref.get("kind"), ref.get("name")
            if not (isinstance(kind, str) and isinstance(name, str)):
                continue
            tns = ref.get("namespace") if isinstance(ref.get("namespace"), str) else ns
            self.deferred.append(("fluxsrc", o, repo, tns, kind, name))

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
        if kcro_class_for(kind, doc) != KIND_CLASS[kind]:
            return                        # same kind name, foreign API group: no pod here
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
            elif tag == "crdtype":
                _, cr, repo, group, kind = ref
                tgt = self.crd_by_gk.get((repo, group, kind))
                if tgt:
                    self.relator("SchemaConformance", cr, tgt)
            elif tag == "fluxsrc":
                _, src, repo, ns, tkind, tname = ref
                tgt = self.flux_src.get((repo, ns, tkind, tname))
                if tgt:
                    self.relator("SourceReference", src, tgt)
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
        relator_classes = {KCRO[c] for c in RELATOR_CLASSES}
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
    unparseable = nonscalar = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            repo = row.get("repo") or row.get("repo_full_name") or "repo"
            try:
                docs = list(yaml.safe_load_all(row["content"]))
            except yaml.YAMLError:      # Helm templates etc.: not parseable as plain YAML
                unparseable += 1
                continue
            for doc in docs:
                if isinstance(doc, dict) and doc.get("kind"):
                    md = doc.get("metadata") or {}
                    if not isinstance(md, dict):
                        nonscalar += 1
                        continue
                    ns, name = md.get("namespace"), md.get("name", "?")
                    # templated fixtures can leave these as dicts/lists: no valid IRI
                    if not all(v is None or isinstance(v, (str, int, float, bool))
                               for v in (ns, name)):
                        nonscalar += 1
                        continue
                    yield repo, doc["kind"], ns, name, doc
    if unparseable:
        print(f"skipped {unparseable} rows that are not parseable YAML", file=sys.stderr)
    if nonscalar:
        print(f"skipped {nonscalar} docs with non-scalar metadata name/namespace", file=sys.stderr)

def load_arrow(path):
    from datasets import load_from_disk          # pip install datasets
    unparseable = nonscalar = 0
    ds = load_from_disk(path)
    for row in ds:                                # *** confirm these column names ***
        try:
            docs = list(yaml.safe_load_all(row["content"]))
        except yaml.YAMLError:      # Helm templates etc.: not parseable as plain YAML
            unparseable += 1
            continue
        for doc in docs:
            if isinstance(doc, dict) and doc.get("kind"):
                md = doc.get("metadata") or {}
                if not isinstance(md, dict):
                    nonscalar += 1
                    continue
                ns, name = md.get("namespace"), md.get("name", "?")
                # templated fixtures can leave these as dicts/lists: no valid IRI
                if not all(v is None or isinstance(v, (str, int, float, bool))
                           for v in (ns, name)):
                    nonscalar += 1
                    continue
                yield row.get("repo_full_name", "repo"), doc["kind"], ns, name, doc
    if unparseable:
        print(f"skipped {unparseable} rows that are not parseable YAML", file=sys.stderr)
    if nonscalar:
        print(f"skipped {nonscalar} docs with non-scalar metadata name/namespace", file=sys.stderr)

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
    if kg.unnameable:
        print(f"skipped {kg.unnameable} new-class objects with no metadata.name or "
              f"generateName", file=sys.stderr)
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