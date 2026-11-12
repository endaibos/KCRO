# KCRO v0.5.0-draft: coverage, relators and consistency on four corpora

All figures are produced by `src/portability_report.py` and `src/consistency_check.py`
from the corpora and ABoxes in this repository. Per-corpus detail is in
`results/portability-<corpus>-v050.json`.

## 1. Coverage by step

Coverage = objects typed by KCRO / objects that carry a `kind`.

| Step | Change | gitops | rahman | kubeobjects | stack |
|---|---|---:|---:|---:|---:|
| v0.3.0 | the 17 original kinds | 38.32% | 69.08% | 80.67% | 84.07% |
| 1 | + `CustomResourceDefinition` | 47.67% | 69.08% | 82.32% | 87.04% |
| 1b | correction: native kind name, foreign API group | 47.56% | 69.04% | 82.27% | 87.04% |
| 3 | + eight Flux / Argo CD kinds | 57.96% | 70.45% | 82.59% | 87.04% |
| 2 | + open `CustomResource` category | **83.02%** | **84.76%** | **94.67%** | **87.04%** |

Steps are numbered as proposed, and were run in the order shown. Step 2 is a
catch-all: run before step 3 it would also absorb the Flux and Argo objects, and
step 3 would then measure no gain.

Step 1b is a correction, not an extension. `serving.knative.dev/Service`,
`projectcalico.org/NetworkPolicy` or `batch.volcano.sh/Job` share a kind name with a
native kind but are different resources (18 objects in gitops, 1 in rahman, 33 in
kubeobjects, 10 in stack). They are no longer typed as the native kind; from step 2
they are custom resources. The rule applies only when the manifest names a DNS-style
group that is not the kind's own: a manifest with no `apiVersion` is an incomplete
native manifest and is typed as in v0.3.0.

Notes on single cells:

- **rahman, step 1:** no gain, the corpus contains no CRDs.
- **stack, steps 3 and 2:** no gain. The corpus contains no Flux or Argo resource and
  only 17 custom resources in 276,520 objects. Its remaining 13% is native kinds
  outside the original 17 (Namespace 10,128, PersistentVolumeClaim 8,837,
  PersistentVolume 3,496, StorageClass 3,090).
- **kubeobjects, step 3:** 239 Flux / Argo objects.

### Why gitops stops at 83%

The open category admits only objects whose API group is neither native Kubernetes nor
a client-side tool. What remains outside on gitops:

| residue | objects |
|---|---:|
| native kinds outside the original 17 (Namespace 423, PersistentVolumeClaim 146, ...) | 1,043 |
| files that are not cluster resources (kustomize build files 1,426, kustomize components, Talos machine configuration, test-harness manifests) | 1,722 |

1,418 of the 1,426 kustomize build files carry no `metadata.name`.

## 2. Relators

### Original relators (the ten classes of v0.3.0)

| corpus | relators, v0.3.0 kinds only | unresolved | relators, v0.5.0 | unresolved |
|---|---:|---:|---:|---:|
| gitops | 3,054 | 108 | 3,054 | 108 |
| rahman | 1,641 | 22 | 1,641 | 22 |
| kubeobjects | 61,158 | 599 | 61,156 | 597 |

"Unresolved" = mediates fewer than two entities. Every unresolved relator, in every
corpus, is a `ClusterRoleBinding` or `RoleBinding` whose role or subject is not in the
corpus. The extension leaves these figures unchanged on gitops and rahman. On
kubeobjects, correction 1b removes two bindings that belong to third-party APIs
(`authorization.openshift.io`), both of them unresolved.

### New relators (v0.5.0)

Both are created only when their target is found, so "unresolved" does not apply to
them. Their quality is the share of references that find a target, which can fail:

| corpus | custom resources | defining CRD in the same repository | defining CRD anywhere in the corpus | Flux source references | resolved in the same repository |
|---|---:|---:|---:|---:|---:|
| gitops | 5,775 | 1,361 (23.6%) | 4,009 (69.4%) | 1,017 | 556 (54.7%) |
| gitops, repositories with Flux / Argo resources (30) | 3,558 | 205 (5.8%) | 825 (23.2%) | 1,017 | 556 (54.7%) |
| gitops, other repositories (27) | 2,217 | 1,156 (52.1%) | 1,275 (57.5%) | 0 | |
| rahman | 424 | 0 (0.0%) | 0 (0.0%) | 0 | |
| kubeobjects | 9,341 | 199 (2.1%) | 6,768 (72.5%) | 50 | 47 (94.0%) |

Custom resources include the eight typed Flux / Argo kinds. The ABox links a custom
resource to a CRD (`kcro:SchemaConformance`) only within one repository; the
"anywhere in the corpus" column is a measurement, not a set of links. Flux source
references are read from `spec.sourceRef`, `spec.chart.spec.sourceRef` and
`spec.chartRef`.

## 3. ABoxes

| corpus | triples |
|---|---:|
| kubeobjects | 922,805 |
| gitops | 92,156 |
| gitops, repositories with Flux / Argo resources | 51,575 |
| gitops, other repositories | 40,609 |
| rahman | 26,110 |

The v0.3.0 ABox `ontology/kcro-abox.ttl` (537,947 triples) is unchanged. The
kubeobjects and stack v0.5.0 ABoxes are too large for the repository and are rebuilt
with `src/instantiate_kcro.py` and `src/build_abox_partitioned.py`.

## 4. Consistency (HermiT, 24 GB heap, TBox with gUFO inlined)

| input | ABox triples | result | reasoning time |
|---|---:|---|---:|
| TBox alone (86 classes) | 0 | consistent, no unsatisfiable class | 0.5 s |
| rahman | 26,110 | consistent | 11.0 s |
| gitops, other repositories | 40,609 | consistent | 23.9 s |
| gitops, repositories with Flux / Argo resources | 51,575 | consistent | 20.6 s |
| gitops | 92,156 | consistent | 65.6 s |
| kubeobjects v0.5.0, in 46 parts of whole repositories | 929,241 in total | all 46 parts consistent | 382.5 s in total |
| kubeobjects (v0.4.0 build), as one ABox | 680,035 | no result after 2,700 s | |

HermiT does not finish on kubeobjects as a single ABox; the limit is its size, since
the TBox alone reasons in half a second. Reference resolution never crosses a
repository, so the corpus was split into 46 groups of whole repositories
(`src/consistency_by_parts.py`), each built with the normal mapper and checked on its
own. All 46 are consistent; the largest has 51,289 triples and took 46 s. This is a
per-part result, not a proof for the union: container images are shared individuals
across parts, which is also why the parts sum to more triples than the single ABox.
The stack ABox was not checked.

Declaring `kcro:namespace` with domain `kcro:KubernetesObject` made the rahman ABox
inconsistent: bindings are relators, relators are aspects in gUFO, and gUFO declares
`Aspect` and `Object` disjoint, while 90 bindings in rahman carry a namespace. The four
manifest-level properties therefore have domain `gufo:Endurant`. Consistency results
obtained before the property was declared did not test this.

## 5. Competency questions

Answers from `src/cq_runner.py`; per-corpus files are `results/cqs-<corpus>.json`.

| | question | kubeobjects v0.3.0 | kubeobjects v0.5.0 | rahman | gitops |
|---|---|---:|---:|---:|---:|
| CQ1 | privileged containers | 284 | 284 | 16 | 13 |
| CQ2 | containers without resource limits | 10,726 | 10,726 | 366 | 497 |
| CQ3 | pods on the default ServiceAccount | 7,485 | 7,485 | 358 | 605 |
| CQ4 | unpinned image tags | 3,687 | 3,687 | 232 | 206 |
| CQ5 | wildcard RBAC rules | 2,425 | 2,420 | 65 | 360 |
| CQ6 | cluster-admin bindings to a ServiceAccount | 74 | 74 | 5 | 11 |
| CQ7 | externally exposed services | 1,156 | 1,156 | 119 | 112 |
| CQ8 | ingresses without TLS | 592 | 591 | 38 | 36 |
| CQ9 | privileged and externally reachable containers | 6 | 6 | 2 | 0 |
| CQ10 | secret-mounting pods behind exposed services | 90 | 90 | 12 | 9 |
| CQ11 | ServiceAccounts of privileged pods with wildcard roles | 34 | 34 | 0 | 0 |
| CQ12 | source repositories hosting a privileged container | not answerable | 140 | 4 | 4 |
| CQ13 | custom resources whose CRD is in the same repository | | 199 | 0 | 1,039 |
| CQ14 | distinct sources that Flux delivery resources pull from | | 41 | 0 | 349 |

CQ1 to CQ11 give the same answers on the v0.5.0 graph as in the thesis, except CQ5 and
CQ8: five `Role` objects of cloud IAM APIs (`iam.aws.upbound.io`,
`iam.services.k8s.aws`) and one `voyager.appscode.com` Ingress are no longer typed as
the native kinds. CQ12 needs the provenance links and is answerable from v0.4.0 on;
all 140 repositories carry star counts. CQ13 and CQ14 are new in v0.5.0 and use the
new relators. CQ9 remains a lower bound: it counts only exposure paths whose Service
selector resolves to a pod in the same repository.
