# KCRO Mapper — `instantiate_kcro.py`

A reusable ETL artefact that turns a corpus of Kubernetes manifests into a
**KCRO knowledge graph** (the ABox), grounded in the gUFO foundational ontology.

This document describes its architecture, the interfaces you extend, and how to
add a new **data source** or a new **mapping rule** — so the script can be reused
and audited as a standalone artefact, independent of any one dataset.

---

## 1. What it does

```
manifests  ──►  pass 1: objects + aspects  ──►  pass 2: resolve references  ──►  kcro-abox.ttl
(YAML/Arrow)        (mint individuals)            (relators between objects)        (Turtle ABox)
```

Each in-scope Kubernetes object becomes an RDF individual. Security findings
become **intrinsic aspects** that `gufo:inheresIn` their bearer; inter-resource
references become **relators** that `gufo:mediates` the entities they connect.
The output imports the KCRO TBox (`owl:imports <https://w3id.org/kcro>`) so it can
be reasoned over (HermiT) and queried with SPARQL.

**Run it:**

```bash
.venv/bin/python instantiate_kcro.py --arrow k8s_dataset --out kcro-abox.ttl --verify
# or:  --jsonl corpus.jsonl
```

`--verify` prints per-class instance counts and the number of relators that
mediate fewer than two entities (unresolved corpus references).

---

## 2. Architecture: two layers

The file fuses two concerns that are conceptually separate. Knowing the seam is
the key to reusing it.

### Layer A — the generic gUFO ABox builder (`KCROGraph` helpers)

Ontology-agnostic machinery that knows *gUFO patterns*, not Kubernetes:

| Helper | gUFO pattern it emits |
|---|---|
| `add_type(indiv, cls)` | `indiv rdf:type kcro:cls` (counted once per identity) |
| `aspect(bearer, cls, key)` | intrinsic aspect: `a rdf:type cls ; gufo:inheresIn bearer ; kcro:datasetKey key` |
| `relator(cls, *mediated)` | relator: `r rdf:type cls ; gufo:mediates m₁ … mₙ` |
| `_h / obj_iri / child_iri` | deterministic SHA1 IRI minting (dedup by identity) |

These would work for **any** UFO-grounded ontology, given different class names.

### Layer B — the Kubernetes → KCRO mapping rules (domain knowledge)

The actual security expertise, currently expressed as imperative Python:

- `KIND_CLASS` / `WORKLOAD_KINDS` — which Kubernetes `kind` maps to which KCRO class.
- `ingest()` dispatch + `_workload / _service / _ingress / _rbac / _binding` —
  every "if this manifest field looks like X, mint aspect/relator Y" decision.
- `resolve()` — the second pass that turns recorded references into relators.

> The mapping **content** (which field is which vulnerability) is the scientific
> contribution and is intentionally domain-specific. The **mechanism** (Layer A)
> is generic.

---

## 3. The two-pass build (and why)

A single pass can't resolve references, because a Service may point at a Pod that
hasn't been read yet. So:

1. **Pass 1 — `ingest()`**: mint every object and its inhering aspects. Record
   each object in `by_name[(repo, ns, kind, name)]` and queue cross-references in
   `self.deferred` (e.g. `("identity", pod, repo, ns, saName)`).
2. **Index — `index_pod_labels()`**: build `pods_by_label[(repo, ns)]` so
   selector-based references (Service/NetworkPolicy → Pod) can be matched by label.
3. **Pass 2 — `resolve()`**: drain `self.deferred`, look targets up in `by_name`
   (or by label), and mint the relator with `relator(cls, subject, target)`.

**Scoping:** references resolve **per `(repo, namespace)`** — a Service in repo A
never wires to a Pod in repo B. (The one cross-repo link that *does* exist is a
shared `ContainerImage`, minted by image digest/string, not by repo.)

**Audit:** `under_mediated()` reports relators with `< 2` participants — gUFO
expects a relator to mediate at least two entities, so these flag corpus
references whose target was outside the corpus.

---

## 4. Determinism & deduplication

IRIs are content-addressed via `_h(*parts)` (12 hex chars of SHA1):

- `obj_iri(repo, kind, ns, name)` → `:Pod-<hash(repo,kind,ns,name)>`
- `child_iri(parent, suffix, *extra)` → `:Container-<hash(...)>`
- aspects/relators are salted by their `datasetKey` and participants.

Re-minting the same logical entity yields the **same IRI**, so the rdflib graph
deduplicates automatically and a re-run is byte-stable. `add_type` counts an
individual only the first time its type triple is new, so `--verify` reports
**distinct individuals**, not occurrences.

---

## 5. The Source interface (add a new dataset)

The mapper consumes a stream of 5-tuples. Any loader that yields this contract
can feed it:

```python
(repo: str, kind: str, namespace: str|None, name: str, doc: dict)
#  doc = the parsed manifest (a single Kubernetes object)
```

Built-in loaders:

- `load_jsonl(path)` — one JSON object per line with a `content` field holding the
  raw (possibly multi-document) YAML.
- `load_arrow(path)` — a HuggingFace Arrow dataset dir; reads `repo_full_name` and
  `content`.
- `load_repo_meta(path)` — a side scan of the Arrow dataset for per-repo GitHub
  stats (`gh_stars/forks/language`), keyed by `repo_full_name`.

**To add a source** (e.g. a directory of `*.yaml`, a `kubectl get -o yaml` dump,
or a live cluster): write a generator that `yaml.safe_load_all`s each document and
yields the 5-tuple above. Wire it in `main()` next to the existing
`load_jsonl/load_arrow` branch. Nothing downstream changes.

```python
def load_yaml_dir(path):
    for f in Path(path).rglob("*.y*ml"):
        for doc in yaml.safe_load_all(f.read_text()):
            if isinstance(doc, dict) and doc.get("kind"):
                md = doc.get("metadata") or {}
                yield path, doc["kind"], md.get("namespace"), md.get("name", "?"), doc
```

> The `namespace` field is used to scope reference resolution per `(repo, namespace)`;
> it is not required to be globally unique.

---

## 6. How to add a mapping rule

### A new object kind
Add it to `KIND_CLASS` (`{ "<k8s kind>": "<KCRO class>" }`), and to
`WORKLOAD_KINDS` if it carries a Pod template. Make sure the KCRO class exists in
the TBox (`kcro.ttl`).

### A new vulnerability aspect
Inside the relevant `_*` method, evaluate the manifest field and mint:

```python
if not psc.get("readOnlyRootFilesystem"):
    self.aspect(cont, "WritableRootFilesystem",
                "container.securityContext.readOnlyRootFilesystem=missing")
```

- `bearer` = the individual the aspect inheres in (Pod for pod-level, Container for
  container-level — mirror the TBox's `inheresIn some <Bearer>` restriction).
- the `datasetKey` literal **must match** the field label `survey.py --security`
  produces in `security_analysis.json`, so `--verify` can diff counts.
- add a `salt=(i,)` when several occurrences may inhere in one bearer (e.g. one
  aspect per RBAC rule), so their IRIs stay distinct.

Then declare the class in the TBox and add it to `ANALYSIS_KEYS` in `srq3.py`.

### A new relator (inter-resource reference)
1. In pass 1, record the reference (don't resolve yet):
   `self.deferred.append(("mytag", subjectIRI, repo, ns, targetKind, targetName))`
2. In `resolve()`, add a branch that looks the target up and mints the relator:
   ```python
   elif tag == "mytag":
       _, subj, repo, ns, tk, tn = ref
       tgt = self.by_name.get((repo, ns, tk, tn))
       if tgt: self.relator("MyRelation", subj, tgt)
   ```
3. Add `MyRelation` to the TBox (subclass of `kcro:KubernetesRelation`) and to the
   `relator_classes` set in `under_mediated()` so it's audited.

---

## 7. Outputs & verification

- **Output:** `kcro-abox.ttl` — Turtle ABox importing the KCRO TBox.
- **`--verify`:** per-class distinct-individual counts + under-mediated relators.
- **`cq_runner.py`:** runs the 12 competency questions as indexed in-memory joins
  (seconds, no SPARQL blow-up) — `python cq_runner.py --abox kcro-abox.ttl --cqs`.
- **`srq3.py`:** full-corpus build + coverage %, triple count, per-class diff vs
  `security_analysis.json`, and the CQ pass in one report.

---

## 8. Generalisation roadmap (future work)

This artefact is documented as-is. To turn it into a reusable **framework**, the
seam in §2 suggests three moves, in increasing effort:

1. **Externalise the ontology binding** — namespaces, the gUFO predicates
   (`inheresIn`/`mediates`/`isProperPartOf`), and the TBox import IRI into a config,
   so the engine can target a new KCRO version or another UFO-grounded ontology.
2. **Make the simple rules declarative** — express the field→aspect / exposure /
   image-tag rules as a YAML rule set interpreted by Layer A, keeping a handful of
   registered **procedural resolvers** for genuinely relational rules (the
   label-selector matching in `resolve()` can't be a flat lookup).
3. **Split the modules** — `engine.py` (generic gUFO builder), `sources.py` (input
   adapters), `kcro_rules.(py|yaml)` (domain rules), and a thin `instantiate_kcro.py`
   CLI. Behaviour (and the emitted ABox) should remain identical.

What stays domain-specific by design: the Kubernetes field knowledge itself. KCRO
is a Kubernetes security ontology, so "map any dataset" means "map any Kubernetes
configuration corpus" — the generic part is the gUFO ABox-construction mechanism,
not the security semantics.

---

## 9. AI disclosure

The mapper's scope, the Kubernetes-to-KCRO mapping decisions, and the gUFO
modelling choices are the author's. Claude (Anthropic) was used as an assistant
for implementing and refactoring the ABox-construction engine (the gUFO
aspect/relator emitters, the two-pass reference resolution, deterministic IRI
minting), for wiring the verification tooling
(`cq_runner.py`, `srq3.py`), and for writing this documentation. All AI-assisted
code was reviewed by the author, and the generated ABox was validated against the
survey counts (`--verify` / `security_analysis.json`) and checked for OWL 2 DL
consistency against the KCRO TBox with HermiT in Protégé.
