# KCRO — Kubernetes Cybersecurity Research Ontology

**The FAIR semantic layer over the [KubeObjects][kubeobjects] corpus: an OWL 2 DL
ontology (KCRO), a deterministic mapping pipeline, and the resulting knowledge
graph.**

The data side of Kubernetes-security research is solved by **KubeObjects**
[Grella et al., 2026][kubeobjects-paper] — a large, deduplicated corpus of
real-world Kubernetes manifests enriched with repository metadata. What did not
yet exist is the **semantic layer** that should sit on top of it: a formal
vocabulary for the security findings, and a queryable knowledge graph that turns
the corpus into machine-reasonable facts. **This repository is that layer.**

> KCRO does **not** modify or re-publish KubeObjects. It builds *on top of* the
> corpus: the manifests remain the authoritative data source (credit and
> provenance below); KCRO adds the ontology, the mapping, and the graph.

### Versions

The thesis (TScIT 45) describes **KCRO v0.3.0 (537,947-triple ABox)** — that is the
exact artefact it evaluates, frozen at the [`v0.3.0`](https://github.com/endaibos/KCRO/releases/tag/v0.3.0)
tag. This repository has **since added the v0.4.0 instance-level provenance layer
(680,035 triples)**; the files on the default branch are v0.4.0. For the
thesis-evaluated snapshot, use the `v0.3.0` tag.

- 🌐 Persistent ontology IRI: **<https://w3id.org/kcro>** (resolves to this repo's `main`)
- 📦 Code & artefacts: **<https://github.com/endaibos/KCRO>**
- 🧱 Built on: **KubeObjects** — <https://github.com/TheGrella/KubeObjects>
- 📄 Licence: **CC BY 4.0**

---

## What's here

This repository adds five things on top of the KubeObjects corpus:

| Artefact | File(s) | What it is |
|---|---|---|
| **KCRO ontology** | [`kcro.ttl`](ontology/kcro.ttl) | The OWL 2 DL TBox — **72 classes** covering Kubernetes resources, security aspects, and inter-resource relators, grounded in [gUFO](http://purl.org/nemo/gufo). Thesis: v0.3.0; current file: v0.4.0-draft (adds the provenance terms). |
| **Knowledge graph** | [`kcro-abox.ttl`](ontology/kcro-abox.ttl) | The instantiated ABox. **Thesis-evaluated v0.3.0: 537,947 triples** (see the `v0.3.0` tag). Current v0.4.0 (with provenance): 680,035 triples / 213,708 individuals. |
| **Mapping pipeline** | [`instantiate_kcro.py`](src/instantiate_kcro.py) | Deterministic corpus → KG mapper (gUFO `inheresIn`/`mediates` patterns, two-pass reference resolution, provenance). See [MAPPER.md](MAPPER.md). |
| **Analysis & query tooling** | [`survey.py`](src/survey.py), [`cq_runner.py`](src/cq_runner.py), [`srq3.py`](src/srq3.py) | `survey.py` = the SRQ1 security-field analysis; `cq_runner.py` = the 12 competency-question SPARQL queries, run as fast indexed joins. |
| **FAIR packaging** | [`kcro.ttl`](ontology/kcro.ttl), [`metadata.py`](corpus/metadata.py) | Persistent `w3id` IRI, CC BY 4.0 licence, Dublin Core / SKOS metadata in the ontology header. |

Optional: an interactive **KG explorer** ([`kcro_server.py`](src/kcro_server.py) +
the `*_visualizer.html` views) — a local SPARQL-backed browser and a GPU
rendering of the full 210k-node graph.

---

## What KCRO adds over KubeObjects

| | KubeObjects (the corpus) | KCRO (this layer) |
|---|---|---|
| **Form** | Tabular manifests + repo metadata | OWL 2 DL ontology + RDF knowledge graph |
| **Security findings** | Implicit in raw YAML fields | First-class typed individuals (e.g. `AbsentRunAsNonRoot`) inhering in their bearer |
| **Relationships** | Not modelled | Resolved relators (`ServiceRouting`, `IdentityAssignment`, …) |
| **Queries** | Pandas / ad-hoc | SPARQL competency questions over a reasoned graph |
| **Provenance** | Repo columns | `prov:wasDerivedFrom` links every object to its source `Repository` |

Coverage of the mapping: **80.7 %** of corpus objects (60,774 / 75,340) fall into
an in-scope KCRO class; the rest are the open long tail (CRDs, etc.).

---

## Reproduce

Requires Python 3 and the dependencies in [`requirements.txt`](requirements.txt)
(plus `datasets`, `rdflib`). The corpus lives in `data/k8s_dataset/` (a HuggingFace
Arrow dataset produced by KubeObjects — see its repository to rebuild it). Run from
the repo root; paths default to the new layout (`ontology/`, `data/`, `results/`).

```bash
# 1. SRQ1 — security-field analysis of the corpus   → results/security_analysis.json
python src/survey.py --security

# 2. Build the knowledge graph (corpus → ABox)      → ontology/kcro-abox.ttl
python src/instantiate_kcro.py --arrow data/k8s_dataset --verify

# 3. Run the 12 competency questions (SRQ3)
python src/cq_runner.py --cqs

#   ...or the one-shot full report (coverage, KG size, count diff, CQs):
python src/srq3.py --cqs

# Explorer (optional): serves web/ + SPARQL over the ABox at http://localhost:8000
python src/kcro_server.py
```

`instantiate_kcro.py` is deterministic (content-addressed IRIs), so a re-run
yields a byte-stable graph. For the mapper's architecture and how to add a new
data source or mapping rule, see **[MAPPER.md](MAPPER.md)**.

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
  note         = {Code: \url{https://github.com/endaibos/KCRO}. ORCID: <ADD ORCID>}
}

@misc{kubeobjects2026,
  author       = {Grella, <ADD AUTHORS>},
  title        = {KubeObjects: A Dataset of Real-World Kubernetes Objects},
  year         = {2026},
  howpublished = {\url{https://github.com/TheGrella/KubeObjects}},
  note         = {DOI: <ADD DOI>}
}
```

> Placeholders `<ADD …>` — fill in your ORCID, the KubeObjects author list and
> paper DOI, and the final thesis citation before publishing.

- **Ontology IRI:** <https://w3id.org/kcro> (persistent; resolves to the repo's `main`)
- **Author:** A. Dorneanu, University of Twente
- **Licence:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

---

## AI use statement

The scope, design decisions, ontology engineering, and literature grounding of
KCRO are the author's. Claude (Anthropic) was used as an assistant for: ontology
review and refactoring (the gUFO meta-typing, the existential bearer restrictions,
and the v0.4.0 provenance extension); implementing and refactoring the mapping
pipeline (the gUFO aspect/relator emitters, two-pass reference resolution,
deterministic IRI minting); formalising and executing the competency-question
SPARQL queries and analysis tooling; and documentation. **All AI-assisted changes
were reviewed by the author.** OWL 2 DL consistency was verified by the author with
HermiT in Protégé, and the generated knowledge graph was validated against the
SRQ1 survey counts (`--verify` / `security_analysis.json`).

[kubeobjects]: https://github.com/TheGrella/KubeObjects
[kubeobjects-paper]: https://github.com/TheGrella/KubeObjects
[kubeobjects-repo]: https://github.com/TheGrella/KubeObjects
