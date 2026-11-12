# Corpus pipeline changelog

Covers everything from the start of the Rahman reconstruction (2026-09-22) through
the three-corpus build and viewer verification (2026-09-27). Written as a record of
what changed and why, so the methodology sections and a reviewer's questions can be
answered from one place.

Environment for every figure below: macOS (darwin 25.6.0), Python 3.14.2 in
`.venv`, rdflib 7.6.0, PyYAML 6.0.3, datasets 4.8.5.

---

## 1. Corpora at a glance

| Corpus | Provenance | Rows | K8s documents | Distinct kinds | ABox triples |
|---|---|---:|---:|---:|---:|
| `rahman` | Reconstructed from the figshare URL tables of Rahman et al. (TOSEM 2023) | 1,654 | 2,697 | 112 | 16,623 |
| `stack` | Downloaded: `substratusai/the-stack-yaml-k8s` | 276,520 | 276,520 | 78 | not built |
| `gitops` | Collected from the GitHub API, 57 repositories | 11,063 | 16,284 | 488 | 30,768 |

All three emit the same JSONL line schema, which is what made one ingest path and
one smoke test possible:

```json
{"repo_full_name": "...", "path": "...", "commit": "...", "corpus": "...",
 "host": "...", "content": "<raw YAML>"}
```

`rahman` adds `deploy`; `stack` and `gitops` add `license` and `stars`.

---

## 2. New files

| File | Purpose |
|---|---|
| `rahman/reconstruct_rahman.py` | Rebuilds the Rahman corpus from `GITHUB-URLS.csv` / `GITLAB-URLS.csv`; disk-cached and resumable |
| `stack/build_stack.py` | Downloads the-stack-yaml-k8s and reshapes its columns to the corpus schema |
| `gitops/collect_gitops.py` | Three-stage GitOps collector (`discover` / `clone` / `build`) with a recorded protocol |
| `src/smoke_corpus.py` | Validates any corpus JSONL before ingest; non-zero exit on a hard failure |
| `gitops/PROTOCOL.json` | Audit trail: queries, dates, method, thresholds, per-repo commit SHAs |
| `gitops/REPOS.csv` | Reviewer-facing list of admitted repositories |
| `gitops/CANDIDATES.json`, `gitops/CLONED.json` | Intermediate discovery and fetch records |

## 3. Changes to existing files

### `src/instantiate_kcro.py`

Four changes, each one a blocker found by actually running the build rather than a
cleanup:

1. **Repo field contract.** `load_jsonl()` read `row["repo"]`, but every producer
   writes `repo_full_name` (only `load_arrow` used that name). Every row therefore
   fell back to the literal string `"repo"`, and since `obj_iri()` hashes
   `(repo, kind, namespace, name)`, objects sharing a kind/namespace/name across
   different repositories minted the same IRI. Measured on the rahman corpus:
   **108 of 1,858 distinct objects (5.8%) silently merged.** Now
   `row.get("repo") or row.get("repo_full_name") or "repo"`, which fixes it for any
   corpus rather than only the ones renamed to suit the loader.
2. **Unparseable YAML.** No guard around `yaml.safe_load_all`, so a single Helm
   template aborted the whole run. 16 such rows exist in the rahman corpus. Now
   counted and skipped, with the count reported on stderr.
3. **Non-scalar object identity.** One test fixture in `emissary-ingress/emissary`
   has `metadata.name: {'self.path.k8s': None}` — a dict, which cannot be a dict key
   in `by_name` nor form an IRI. Documents whose `name`/`namespace` are not scalars
   are now skipped with a count.
4. **Docstring.** The `--jsonl` input description now matches what the loader reads.

`load_arrow()` was deliberately left alone: it is not on this path, though it has the
same missing guards if the main KubeObjects Arrow corpus is ever fed through it.

### `rahman/reconstruct_rahman.py`

- **URL encoding.** One of the 2,039 URL rows points at `resources/traefik rbac.yaml`,
  and `http.client` rejects a request line containing a space
  (`InvalidURL: URL can't contain control characters`). Added `enc()`, which
  percent-encodes repo and path with `%` in the safe set so already-escaped URLs are
  not double-encoded. Plain ASCII paths encode byte-identically, so the existing
  download cache stayed valid.
- **Error resilience.** `except (HTTPError, URLError, TimeoutError)` did not catch
  `InvalidURL` (an `HTTPException`/`ValueError`, not a `URLError`), so one malformed
  row killed a run that was otherwise nearly complete. Widened to
  `(OSError, HTTPException, ValueError)`.

### `.kg_explorer_v030/src/kcro_server.py`

- `ABOX` was hardcoded to the explorer's own `ontology/kcro-abox.ttl`. It now reads
  `KCRO_ABOX`, with `KCRO_PORT` alongside it, so one checkout serves any corpus's
  ABox without copying files.
- `/pods` returned an empty list for a single-corpus ABox: the hero page hardcodes
  `?min=15`, and the best-connected Pod in the gitops graph has degree 13. It now
  falls back to the top-N by degree instead of nothing.

Note that `.kg_explorer_v030/` is gitignored as ephemeral, so these two edits are
untracked.

### `.gitignore`

Added the build caches (`rahman/.cache/`, `gitops/.cache/`) and the two large
generated corpora (`stack/corpora/*.jsonl` at 947 MB, `gitops/corpora/*.jsonl` at
145 MB). `rahman/corpora/rahman.jsonl` (7.4 MB) is not published either: the corpus
files hold third-party manifests verbatim, and the Rahman set contains a credential
(a Grafana API key in one manifest). 33 of its source URLs are already dead, so an
archived copy needs a decision on redaction before it can be deposited anywhere.

---

## 4. Rahman reconstruction results

Of 2,039 URL rows: **1,985 fetched (97.4%)**, 331 of those duplicate
`(repo, commit, path)`, leaving **1,654 unique files**. 54 failures, all upstream
decay rather than pipeline faults, logged with URLs in `corpora/rahman.failed.tsv`:

- 33 × GitHub `404` — repository deleted, made private, or force-pushed since 2022
- 21 × GitLab `403` — GitLab answers Forbidden rather than 404 for removed projects

Three years of link rot between their collection and ours. Worth one sentence in the
methodology, since it means the corpus cannot be reconstructed identically again.

Composition notes that matter for reported counts:

- **9 rows have empty content.** Re-fetched to confirm: genuinely 0-byte files
  upstream, not truncated downloads. They inflate a row count but contribute nothing.
- **16 rows are unparseable YAML and 207 contain no `kind`** — Helm templates,
  `values.yaml`, `kustomization.yaml`, `skaffold.yaml`. Legitimately present in
  Rahman's tables, silently skipped by the loader.
- Effective manifest yield: **1,438 of 1,654 rows**.
- `deploy` is `True` for all 1,645 non-empty rows. All 57 repositories appear in
  `DEPLOY-REPOS.csv` under a `-DEPLOY` column, none under `-NODEPLOY`, and no two
  owners share a repository short name — so the short-name lookup is unambiguous here.

---

## 5. GitOps collection protocol

The only collected rather than reconstructed corpus, so each stage records its own
provenance in `PROTOCOL.json`. Ran 2026-09-27, 16:18–16:31 UTC.

**Discovery** — `GET /search/repositories`, sorted by stars descending, five queries
with `fork:false archived:false` inside the query so the exclusion is part of what a
reviewer can replay:

| Query | Hits |
|---|---:|
| `topic:gitops stars:>=10` | 612 |
| `topic:argocd stars:>=5` | 391 |
| `topic:fluxcd stars:>=5` | 148 |
| `topic:argo-cd stars:>=5` | 36 |
| `topic:flux-cd stars:>=5` | 1 |

**970 unique candidates.**

**Fetch** — 100 repositories, 0 failures, 7 skipped above the 300 MB size cap,
247 MB of YAML. Each repository's default branch was resolved to an immutable commit
SHA via `GET /repos/{repo}/commits/{branch}`, then the tarball fetched at that SHA.

> **Deviation from the intended method.** Shallow `git clone` was planned and is still
> the script's preferred path. It was impossible on this machine: the only `git` is
> Apple's Xcode shim, which refuses to run until `sudo xcodebuild -license` is
> accepted, and all 953 first-attempt clones failed on that rather than on anything
> network-related. The tarball-at-a-pinned-SHA fallback yields the same tree a depth-1
> clone would, with the commit pinned explicitly instead of implied by HEAD. The method
> used is recorded per run in `PROTOCOL.json`; `--force-tarball` forces it, and the
> script switches back to `git clone` automatically once git works.

**Admission** — a repository enters the corpus only if it is not a fork and not
archived (query-time), is within the size cap, contains at least 10 Kubernetes
manifests (a YAML document with both `apiVersion` and `kind`), *and* either carries a
GitOps topic or contains a Flux `HelmRelease`/`Kustomization` or an Argo CD
`Application`.

Result: **57 admitted, 43 rejected**, 11,063 manifest files, 16,284 documents,
488 distinct kinds, 1,284 GitOps CRDs. Top kind is `Kustomization` (1,921).
Licences: Apache-2.0 41, MIT 5, GPL-3.0 3, WTFPL 2, MPL-2.0 2, none 2.
All 43 rejections were `below min-manifests`, mostly tool source repositories
(`terraform-provider-argocd`, `_Book_k8sInfra`) rather than configuration repositories.

**Two weaknesses a reviewer will find, stated plainly:**

1. **All 57 were admitted by topic; the CRD arm never fired.** Because discovery is
   itself topic-based, every candidate already carried a topic, so the
   "or contains a HelmRelease/Application" clause did no independent work. Exercising
   it needs `/search/code` as a second discovery source.
2. **Only 30 of the 57 actually contain a Flux or Argo CRD.** The other 27 carry a
   `gitops` topic but no controller manifests — tutorials, demos, tooling. Tightening
   the rule to AND yields 30 repositories, below the 50 floor; adding code-search
   discovery would instead raise the count of genuine GitOps repositories.

---

## 6. Verification

`src/smoke_corpus.py` checks, cheapest first: every line parses as JSON and carries
the required keys; the YAML parses and looks like Kubernetes; the field names match
what the loader reads; and it prints kind/host/deploy distributions. Non-zero exit on
a hard failure. It was the loader-contract check that caught the `repo` /
`repo_full_name` mismatch before it reached a published number.

| Check | rahman | stack | gitops |
|---|---|---|---|
| Malformed JSON lines | 0 | 0 | 0 |
| Duplicate `(repo, commit, path)` | 0 | 0 | 0 |
| YAML parse errors | 16 | 0 | 0 |
| Rows without `kind` | 207 | 0 | 0 |
| Empty content | 9 | 0 | 0 |
| Loader field contract | ok | ok | ok |

The stack corpus was checked over all 276,520 rows, not a sample: it is pre-filtered
upstream to exactly one valid manifest per row.

**ABox builds.** rahman 16,623 triples (22 relators mediating < 2); gitops 30,768
triples (108). The stack ABox was not built — it is roughly 100× the rahman corpus.

**Viewer.** Both new ABoxes were served and every endpoint exercised over HTTP:

```bash
cd .kg_explorer_v030
KCRO_ABOX=../ontology/kcro-abox-gitops.ttl python src/kcro_server.py   # then :8000
```

| | gitops | rahman |
|---|---|---|
| `/full` | 12,155 nodes / 10,639 edges | 6,481 / 6,074 |
| `/overview` | 50 classes, 56 edges | 46 classes, 50 edges |
| `/`, `/full2d`, `/full3d`, `/node`, `/pods` | 200 | 200 |

All three hero CONSTRUCT queries return results against gitops (CQ3: 24 nodes /
23 links). Node groups for gitops: 5,250 Objects, 3,866 Vulnerabilities,
3,039 Relators.

At 6k–12k nodes these graphs are two orders of magnitude below the 208k-node ABox the
renderer's scaling work targeted, so none of those constraints apply. Only the stack
corpus would reach that scale.

---

## 7. Second round: portability study and the v0.5.0-draft extension

Sections 1 to 6 describe the state on 2026-09-27. Everything below came after, on
branch `corpus-pipeline`. Figures are in
[results/portability-table-v050.md](results/portability-table-v050.md).

**Tooling restored.** A conda-forge `git` (`/opt/anaconda3/envs/gittools/bin/git`)
replaces the Xcode shim, so the work is committed. `src/compute_layout.py` was
recovered from history.

**Regression guard.** The Arrow path reads `repo_full_name`, which exists and is never
null (3,006 repositories), so the repo-key bug of section 3 never touched a published
figure. Rebuilding KubeObjects unchanged gave 537,947 triples, byte-identical.

**Portability study** (`src/portability_report.py`). With the 17 kinds of v0.3.0,
coverage was 80.7% KubeObjects, 84.1% stack, 69.1% Rahman, 38.3% GitOps, and relator
resolution 96.5 to 99.0%. Splitting the GitOps corpus into the 30 repositories with
Flux/Argo resources and the 27 without showed no difference in resolution.

**Provenance merged** (v0.4.0). KubeObjects grows to 680,035 triples, additive only.
`git` merged `src/instantiate_kcro.py` without a conflict but silently dropped the
provenance code, so the file was rebuilt from the branch side with this branch's edits
re-applied.

**Extension to custom resources** (v0.5.0-draft), in the order it was measured:

| stage | change | gitops | rahman | kubeobjects | stack |
|---|---|---:|---:|---:|---:|
| start | 17 kinds | 38.32% | 69.08% | 80.67% | 84.07% |
| S0 | declare six terms the ABoxes already used | same | same | same | same |
| S1 | `CustomResourceDefinition` under `ExtensionResource` | 47.67% | 69.08% | 82.32% | 87.04% |
| S1b | correction: reject native kind names from foreign API groups | 47.56% | 69.04% | 82.27% | 87.04% |
| S3 | eight Flux / Argo CD kinds, `SourceReference` | 57.96% | 70.45% | 82.59% | 87.04% |
| S2 | open `CustomResource` category, `SchemaConformance` | 83.02% | 84.76% | 94.67% | 87.04% |

Design points worth knowing before changing any of it:

- `KIND_CLASS` and `obj_iri` are untouched, so the 17 kinds mint the same IRIs as in
  v0.3.0. New kinds go in `GROUP_KIND_CLASS`, keyed on API group and kind.
- The open category admits only third-party API groups. Native kinds outside the 17
  (Namespace, PersistentVolumeClaim, PodDisruptionBudget and others) and client-side
  files (kustomize build files, Talos machine config, test harness manifests) stay out,
  which is why GitOps stops at 83% and not 100%.
- The eight GitOps classes are subclasses of `kcro:CustomResource` only. An earlier design had
  also placed each under one of the five functional groupings; that was not done,
  because none of the five fits a source object such as `GitRepository` and the
  groupings are mutually disjoint. **This is an open modelling decision.**
- New relators are minted only when their target is found, so they are never
  under-mediated. They are reported apart from the original relators, as the share of
  references that find a target. Every custom resource, including the eight typed
  kinds, is checked against its defining CRD.
- An object that shares only its kind name with a native kind (a Knative `Service`)
  is typed as a custom resource and gets none of the native kind's rules.
- `kcro:namespace`, `apiGroup`, `apiVersion` and `resourceKind` have domain
  `gufo:Endurant`. With domain `kcro:KubernetesObject` the Rahman ABox was
  inconsistent, because bindings are relators and gUFO makes Aspect and Object
  disjoint. Earlier "consistent" verdicts were obtained while `kcro:namespace` was
  undeclared, so they did not test this.

**Consistency.** No unsatisfiable class among the 86. Rahman, GitOps and both GitOps
sub-corpora are consistent on the v0.5.0 builds. KubeObjects does not finish as one
ABox (45 minutes, 24 GB heap), but split into 46 groups of whole repositories every
group is consistent (`src/consistency_by_parts.py`).

**Competency questions.** CQ12 is answered (140 repositories on KubeObjects); it had
stayed a placeholder after the provenance merge. CQ13 and CQ14 query the new relators.
CQ1 to CQ11 are unchanged on KubeObjects apart from six third-party objects no longer
typed as native kinds.

## 7b. Known gaps

- **Not released.** Version metadata says `0.5.0-draft`; nothing is pushed, tagged or
  deposited. The w3id still resolves to `main` (v0.3.0) without content negotiation.
- **No single-ABox consistency result** for KubeObjects (per-part only) and none for
  stack.
- **CQ9 is a lower bound**, and KubeObjects is deduplicated within repositories, not
  across them. Both are properties of the corpus and are left as stated limitations.
- **No alignment axioms** to UCO, STIX or CWE, no public SPARQL endpoint, no content
  negotiation at the w3id, no DOI yet (`CITATION.cff` and `.zenodo.json` are prepared).
- **GitOps corpus selection.** All 57 repositories were admitted by GitHub topic; the
  rule's second arm (contains a Flux or Argo resource) never admitted one by itself.
- **Vulnerability classes are unchanged.** Custom resources are typed and linked, but
  no security aspect is derived from them.
- **The viewer was checked over HTTP only**, never in a browser, and
  `results/full_positions.bin` predates the new graphs.

## 8. Reproduction

```bash
# rahman  (cache makes a re-run cheap; the output file is truncated on each run)
cd rahman && python reconstruct_rahman.py GITHUB-URLS.csv GITLAB-URLS.csv \
    --out corpora/rahman.jsonl --cache .cache/rahman \
    --deploy DEPLOY-REPOS.csv --pause 0.5

# stack
cd stack && python build_stack.py --out corpora/stack.jsonl

# gitops  (needs an authenticated `gh`)
cd gitops
python collect_gitops.py discover --target 100
python collect_gitops.py clone --target 100 --max-size-mb 300
python collect_gitops.py build --out corpora/gitops.jsonl --min-manifests 10

# validate, then ingest
python src/smoke_corpus.py <corpus>/corpora/<corpus>.jsonl [--sample N]
python src/instantiate_kcro.py --jsonl <corpus>/corpora/<corpus>.jsonl \
    --out ontology/kcro-abox-<corpus>.ttl --verify
```

Use `.venv/bin/python` if the shell's `python` is not the project venv — anaconda's
base interpreter has no `rdflib`.
