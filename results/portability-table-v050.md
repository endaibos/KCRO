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
| 1b | correction: native kind name, foreign API group | 47.56% | 69.00% | 82.25% | 87.04% |
| 3 | + eight Flux / Argo CD kinds | 57.96% | 70.41% | 82.56% | 87.04% |
| 2 | + open `CustomResource` category | **83.02%** | **84.72%** | **94.64%** | **87.04%** |

Steps are numbered as proposed, and were run in the order shown. Step 2 is a
catch-all: run before step 3 it would also absorb the Flux and Argo objects, and
step 3 would then measure no gain.

Step 1b is a correction, not an extension. `serving.knative.dev/Service`,
`projectcalico.org/NetworkPolicy` or `batch.volcano.sh/Job` share a kind name with a
native kind but are different resources (18 objects in gitops, 2 in rahman, 54 in
kubeobjects, 12 in stack). They are no longer typed as the native kind; from step 2
they are custom resources.

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
| rahman | 1,641 | 22 | 1,638 | 22 |
| kubeobjects | 61,158 | 599 | 61,122 | 597 |

"Unresolved" = mediates fewer than two entities. Every unresolved relator, in every
corpus, is a `ClusterRoleBinding` or `RoleBinding` whose role or subject is not in the
corpus. The extension leaves these figures unchanged on gitops. The small differences
on rahman and kubeobjects come from correction 1b, which removes relators that had been
built from foreign-group objects.

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
| kubeobjects | 922,497 |
| gitops | 92,156 |
| gitops, repositories with Flux / Argo resources | 51,575 |
| gitops, other repositories | 40,609 |
| rahman | 26,074 |

The v0.3.0 ABox `ontology/kcro-abox.ttl` (537,947 triples) is unchanged. The
kubeobjects and stack v0.5.0 ABoxes are too large for the repository and are rebuilt
with `src/instantiate_kcro.py` and `src/build_abox_partitioned.py`.

## 4. Consistency (HermiT, 24 GB heap, TBox with gUFO inlined)

| input | ABox triples | result | reasoning time |
|---|---:|---|---:|
| TBox alone (86 classes) | 0 | consistent, no unsatisfiable class | 0.5 s |
| rahman | 26,074 | consistent | 11.5 s |
| gitops, other repositories | 40,609 | consistent | 23.9 s |
| gitops, repositories with Flux / Argo resources | 51,575 | consistent | 20.6 s |
| gitops | 92,156 | consistent | 73.1 s |
| kubeobjects (v0.4.0 build) | 680,035 | no result after 2,700 s | |

The limit on kubeobjects is the size of the ABox: the TBox alone reasons in half a
second. The larger v0.5.0 kubeobjects and stack ABoxes were not attempted.

Declaring `kcro:namespace` with domain `kcro:KubernetesObject` made the rahman ABox
inconsistent: bindings are relators, relators are aspects in gUFO, and gUFO declares
`Aspect` and `Object` disjoint, while 90 bindings in rahman carry a namespace. The four
manifest-level properties therefore have domain `gufo:Endurant`. Consistency results
obtained before the property was declared did not test this.
