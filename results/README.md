# Results

Start with [`portability-table-v050.md`](portability-table-v050.md). It is the summary
of the 0.5.0 evaluation: coverage, relators, graph sizes, consistency and competency
questions on the four corpora. Every figure in it comes from one of the files below.

## Evaluation of 0.5.0

| File | Produced by | Content |
|---|---|---|
| `portability-<corpus>-v050.json` | `src/portability_report.py` | Objects, coverage, relators (original and new, per class), custom resources and their CRDs, Flux source references. |
| `cqs-<corpus>-v050.json` | `src/cq_runner.py --cqs --out` | Answers to the 14 competency questions. |
| `cqs-kubeobjects-v030.json` | `src/cq_runner.py --cqs --out` | The same questions on the released 0.3.0 graph, for comparison. |
| `consistency-<corpus>-v050.json` | `src/consistency_check.py` | HermiT verdict, reasoning time and unsatisfiable classes for the ontology with one graph. |
| `consistency-tbox-only.json` | `src/consistency_check.py` | The same check for the ontology alone. |
| `consistency-kubeobjects-v050-by-parts.json` | `src/consistency_by_parts.py` | KubeObjects checked in 46 groups of whole repositories. |
| `consistency-kubeobjects.json` | `src/consistency_check.py` | KubeObjects as a single graph (the 0.4.0 build): no result after 2,700 s. Kept because the summary table reports it. |
| `census-stack-v050.json` | `src/build_abox_partitioned.py` | Individuals per class in the Stack graph. |

`<corpus>` is one of `kubeobjects`, `rahman`, `stack`, `gitops`. The GitOps corpus
also has two subsets: `gitops-crd` (repositories that contain Flux or Argo CD
resources) and `gitops-topic-only` (the other repositories).

## Analysis of 0.3.0

| File | Produced by | Content |
|---|---|---|
| `security_analysis.json` | `src/survey.py --security` | Security-field survey of the KubeObjects corpus. |
| `srq3_report.md` | `src/srq3.py` | Coverage, graph size and competency questions for 0.3.0. |
| `verify-kubeobjects.txt` | `src/instantiate_kcro.py --verify` | Class counts of a rebuild restricted to the 0.3.0 scope, which gives the published 537,947 triples. |

## Support files

| File | Content |
|---|---|
| `gufo.ttl` | Local copy of gUFO, so the consistency checks run offline. |
| `layout_positions.json`, `full_positions.bin`, `full_positions_2d.bin` | Node positions for the graph explorer (`src/compute_layout.py`), computed for the 0.3.0 graph. |

## `earlier-runs/`

Outputs from before the final 0.5.0 run. They are kept for the record and are not
used by the summary table.

- Files without a suffix were measured with the 17 kinds of 0.3.0, before the
  extension. `portability-table.md` and `consistency-table.md` summarise them.
- Files ending in `-s1`, `-s2`, `-s3` were measured after single steps of the
  extension: step 1 adds `CustomResourceDefinition`, step 3 adds the Flux and Argo CD
  kinds, step 2 adds the open `CustomResource` category. The steps were run in the
  order 1, 3, 2.

The matching graphs are in `ontology/earlier-runs/`.
