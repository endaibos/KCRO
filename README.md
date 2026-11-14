# KCRO — Kubernetes Cybersecurity Research Ontology

**A FAIR semantic layer for Kubernetes configuration security: an OWL 2 DL
ontology (KCRO), a deterministic mapping pipeline, and knowledge graphs built from
the [KubeObjects][kubeobjects] corpus and three further corpora.**

The data side of Kubernetes-security research is solved by **KubeObjects**
[Grella et al., 2026][kubeobjects-paper] — a large, deduplicated corpus of
real-world Kubernetes manifests enriched with repository metadata. What did not
yet exist is the **semantic layer** that should sit on top of it: a formal
vocabulary for the security findings, and a queryable knowledge graph that turns
the corpus into machine-reasonable facts. **This repository is that layer.**

> KCRO does **not** modify or re-publish KubeObjects. It builds *on top of* the
> corpus: the manifests remain the authoritative data source (credit and
> provenance below); KCRO adds the ontology, the mapping, and the graph.

## Versions

| Version | Where | What it is |
|---|---|---|
| **0.3.0** | branch `main`, tag [`v0.3.0`](https://github.com/endaibos/KCRO/releases/tag/v0.3.0) | The released ontology (72 classes) and the knowledge graph built from KubeObjects (537,947 triples), as evaluated in the thesis (TScIT 45). |
| 0.4.0 | branch `v0.4.0-provenance` | Experimental. Adds instance-level provenance (source repository, namespace) and the graph explorer. Superseded by 0.5.0, which contains it. |
| **0.5.0-draft** | branch `v0.5.0-custom-resources` (this branch) | Extends the ontology to Kubernetes custom resources (86 classes) and evaluates it on four corpora. Not released yet. |

- Persistent ontology IRI: <https://w3id.org/kcro>. It resolves to `main`, so it serves 0.3.0 until 0.5.0 is released.
- Code and artefacts: <https://github.com/endaibos/KCRO>
- Built on KubeObjects: <https://github.com/TheGrella/KubeObjects>
- Licence: CC BY 4.0

### What 0.5.0 adds

- `CustomResourceDefinition` and an open `CustomResource` category, so objects of
  third-party kinds are typed, not dropped.
- Eight Flux and Argo CD delivery kinds (`HelmRelease`, `GitRepository`,
  `ArgoApplication`, ...).
- Two relators: `SchemaConformance` (a custom resource and the CRD that declares its
  type) and `SourceReference` (a Flux delivery resource and the source it pulls from).
- Declarations for the provenance terms (`prov:wasDerivedFrom`, `kcro:Repository`,
  `kcro:namespace`).
- Classes are selected by the pair of API group and kind, because kind names are
  reused across projects.

The 72 classes of 0.3.0 and the IRIs of their individuals are unchanged. The 0.3.0
graph `ontology/kcro-abox.ttl` is kept as released.

### Results on four corpora

| Corpus | Objects | Coverage, 0.3.0 | Coverage, 0.5.0 | Original relators resolved | Graph (triples) |
|---|---:|---:|---:|---:|---:|
| KubeObjects | 75,340 | 80.7% | 94.7% | 99.0% | 922,805 |
| Rahman et al. | 2,697 | 69.1% | 84.8% | 98.7% | 26,110 |
| the-stack-yaml-k8s | 276,520 | 84.1% | 87.0% | 96.9% | 4,224,527 |
| GitOps repositories | 16,284 | 38.3% | 83.0% | 96.5% | 92,156 |

Coverage is the share of objects that KCRO assigns a class. The full figures, with
the gain of each step, the new relators and the consistency checks, are in
[`results/portability-table-v050.md`](results/portability-table-v050.md). How each
corpus was obtained is in [`CHANGELOG-CORPORA.md`](CHANGELOG-CORPORA.md).

---

## Repository layout

| Path | Content |
|---|---|
| [`ontology/kcro.ttl`](ontology/kcro.ttl) | The ontology (OWL 2 DL, 86 classes, grounded in [gUFO](http://purl.org/nemo/gufo)). |
| [`ontology/kcro-abox.ttl`](ontology/kcro-abox.ttl) | The released 0.3.0 knowledge graph (537,947 triples). |
| `ontology/kcro-abox-*-v050.ttl` | The 0.5.0 graphs of the Rahman and GitOps corpora. The KubeObjects and Stack graphs are too large for the repository and are rebuilt (see below). |
| [`src/`](src/) | The mapper and the evaluation scripts (table below). |
| [`results/`](results/) | Outputs of the evaluation. Start with [`results/README.md`](results/README.md). |
| [`data/k8s_dataset/`](data/k8s_dataset/) | The KubeObjects corpus (Arrow, stored with Git LFS). |
| [`rahman/`](rahman/), [`stack/`](stack/), [`gitops/`](gitops/) | One builder script per additional corpus, with its input lists and collection protocol. |
| [`web/`](web/), [`figures/`](figures/) | The graph explorer pages and the figures. |
| [`corpus/`](corpus/) | The KubeObjects scraper, kept from the upstream repository. |
| [`archive/`](archive/), `*/earlier-runs/` | Superseded scripts and outputs, kept for the record. |

| Script | Purpose |
|---|---|
| [`instantiate_kcro.py`](src/instantiate_kcro.py) | The mapper: corpus to knowledge graph. Documented in [MAPPER.md](MAPPER.md). |
| [`build_abox_partitioned.py`](src/build_abox_partitioned.py) | The same build, one repository group at a time, for corpora too large for memory. |
| [`smoke_corpus.py`](src/smoke_corpus.py) | Checks a corpus file before it is mapped. |
| [`portability_report.py`](src/portability_report.py) | Coverage and relator resolution per corpus. |
| [`cq_runner.py`](src/cq_runner.py) | Answers the 14 competency questions. |
| [`consistency_check.py`](src/consistency_check.py), [`consistency_by_parts.py`](src/consistency_by_parts.py) | HermiT consistency of the ontology with one graph, or with a large graph split by repository. |
| [`survey.py`](src/survey.py), [`srq3.py`](src/srq3.py), [`verify_counts.py`](src/verify_counts.py) | The 0.3.0 analysis: security-field survey, full report, and a check that the released graph still has its published counts. |
| [`kcro_server.py`](src/kcro_server.py), [`compute_layout.py`](src/compute_layout.py) | Local SPARQL server and layout for the graph explorer. |

---

## Reproduce

Requires Python 3 and [`requirements.txt`](requirements.txt). The consistency checks
also need a Java runtime. Run everything from the repository root.

Always pass `--out` to the mapper. Without it the mapper writes to
`ontology/kcro-abox.ttl` and replaces the released 0.3.0 graph.

```bash
# 1. Build the 0.5.0 graph of KubeObjects
python src/instantiate_kcro.py --arrow data/k8s_dataset \
    --out ontology/kcro-abox-kubeobjects-v050.ttl --verify

# 2. Coverage and relator resolution
python src/portability_report.py --corpus kubeobjects --arrow data/k8s_dataset \
    --abox ontology/kcro-abox-kubeobjects-v050.ttl \
    --out results/portability-kubeobjects-v050.json

# 3. The 14 competency questions
python src/cq_runner.py --cqs --abox ontology/kcro-abox-kubeobjects-v050.ttl \
    --out results/cqs-kubeobjects-v050.json

# 4. Consistency (HermiT): a graph that ships with the repository, then KubeObjects by parts
python src/consistency_check.py --abox ontology/kcro-abox-rahman-v050.ttl --corpus rahman-v050
python src/consistency_by_parts.py --arrow data/k8s_dataset --corpus kubeobjects-v050

# 5. Check that the released 0.3.0 graph is unchanged (reads only)
python src/verify_counts.py
```

The three additional corpora are not stored here. They contain third-party manifests
verbatim, and one of them includes a credential that its original author committed.
Each is rebuilt by the script in its folder; the commands are in
[section 8 of the changelog](CHANGELOG-CORPORA.md#8-reproduction). A rebuilt corpus is
then mapped with `--jsonl <corpus>/corpora/<corpus>.jsonl` in place of `--arrow`.
The GitOps corpus is pinned to commit SHAs recorded in
[`gitops/PROTOCOL.json`](gitops/PROTOCOL.json).

The mapper is deterministic: IRIs are derived from content, so a second run gives
the same graph.

To browse a graph locally:

```bash
KCRO_ABOX=ontology/kcro-abox-gitops-v050.ttl python src/kcro_server.py   # http://localhost:8000
```

---

## Citation

If you use KCRO, please cite both the ontology/thesis **and** the underlying
corpus:

```bibtex
@misc{kcro2026,
  author       = {Dorneanu, Andrei},
  title        = {KCRO: Kubernetes Cybersecurity Research Ontology},
  year         = {2026},
  howpublished = {\url{https://w3id.org/kcro}},
  note         = {Code: \url{https://github.com/endaibos/KCRO}. ORCID: 0009-0000-0969-772X}
}

@misc{kubeobjects2026,
  author       = {Grella and Aliforenko and Mariot},
  title        = {KubeObjects: A Dataset of Real-World Kubernetes Objects},
  year         = {2026},
  howpublished = {\url{https://github.com/TheGrella/KubeObjects}},
  note         = {MSR 2026. DOI: 10.1145/3793302.3793317}
}
```

> The KubeObjects `@misc` above is for repo convenience. In a paper, cite the
> published MSR proceedings entry (`@inproceedings`, DOI `10.1145/3793302.3793317`)
> rather than this GitHub-URL form.

- **Ontology IRI:** <https://w3id.org/kcro>
- **Author:** A. Dorneanu, University of Twente
- **Licence:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

---

## AI use statement (release 0.3.0)

The scope, design decisions, ontology engineering, and literature grounding of
KCRO are the author's. Claude (Anthropic) was used as an assistant for: ontology
review and refactoring (the gUFO meta-typing, the existential bearer restrictions);
implementing and refactoring the mapping pipeline (the gUFO aspect/relator
emitters, two-pass reference resolution, deterministic IRI minting); formalising
and executing the competency-question
SPARQL queries and analysis tooling; and documentation. **All AI-assisted changes
were reviewed by the author.** OWL 2 DL consistency was verified by the author with
HermiT in Protégé, and the generated knowledge graph was validated against the
SRQ1 survey counts (`--verify` / `security_analysis.json`).

[kubeobjects]: https://github.com/TheGrella/KubeObjects
[kubeobjects-paper]: https://doi.org/10.1145/3793302.3793317
[kubeobjects-repo]: https://github.com/TheGrella/KubeObjects
