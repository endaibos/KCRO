| Input | ABox triples | Verdict | HermiT wall-clock |
|---|---:|---|---:|
| tbox-only | 0 | **consistent** | 0.4s |
| gitops-topic-only | 19,027 | **consistent** | 10.0s |
| gitops-crd | 19,501 | **consistent** | 9.7s |
| rahman | 19,803 | **consistent** | 9.8s |
| gitops | 38,500 | **consistent** | 33.8s |
| kubeobjects | 680,035 | timeout | 2700.2s |

All runs: HermiT via owlready2 0.51, 24 GB heap, TBox + gUFO inlined.
kubeobjects did not terminate within 2700s. The TBox alone is consistent in 0.4s,
so the limit is ABox scale and not the ontology.
stack (2,897,434 triples) was not attempted: it is ~4x kubeobjects, which already
exceeded the budget.
