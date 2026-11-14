#!/usr/bin/env python3
"""
portability_report.py --- coverage and resolution figures per corpus.

`instantiate_kcro.py --verify` prints a class census but not the numbers the
portability claim rests on: how much of a corpus KCRO's 17 sortal kinds actually
cover, and how that interacts with relator resolution. This produces both, per
corpus, as JSON plus one Markdown table row.

    python src/portability_report.py --corpus rahman \
        --jsonl rahman/corpora/rahman.jsonl --abox ontology/kcro-abox-rahman-v050.ttl
    python src/portability_report.py --corpus kubeobjects \
        --arrow data/k8s_dataset --abox ontology/kcro-abox-kubeobjects-v050.ttl
    python src/portability_report.py --table results/portability-*-v050.json

Counting logic is reused, not restated: the class census counts distinct
rdf:type subjects, which is exactly the quantity KCROGraph.add_type() maintains
in .counts; under-mediation comes from KCROGraph.under_mediated() itself, called
on the parsed ABox; and RELATOR_CLASSES / KIND_CLASS are imported.

Read coverage and resolution together. Resolution is computed only over in-scope
objects, so a corpus with a large unmapped tail can post a high resolution rate
simply because the objects that would have dangled were never instantiated.
"""
import argparse, json, sys, collections
from pathlib import Path

import yaml
from rdflib import Graph, RDF, RDFS, OWL, URIRef

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from instantiate_kcro import (          # the logic this report must not restate
    KCRO, KIND_CLASS, GROUP_KIND_CLASS, RELATOR_CLASSES, NEW_RELATOR_CLASSES,
    CUSTOM_RESOURCE_CLASSES, FLUX_SOURCE_CLASSES, KCROGraph,
    kcro_class_for, api_group,
)

ORIGINAL_RELATOR_CLASSES = tuple(c for c in RELATOR_CLASSES if c not in NEW_RELATOR_CLASSES)

# The non-sortal catch-alls. Both are counted: rdflib does no subclass reasoning, so
# individuals typed kcro:CustomResource would not show up under KubernetesObject.
OPEN_CLASSES = ("KubernetesObject", "CustomResource")
TBOX = ROOT / "ontology" / "kcro.ttl"


# ----------------------------------------------------------------- corpus side
def corpus_stats(docs_iter):
    """rows/documents/parse_failures/no_kind/objects/in_scope + the unmapped tail.

    Scope is decided by kcro_class_for(), the same authority ingest() uses, so the
    corpus-side coverage figure and the ABox cannot diverge once a class is keyed on
    (group, kind) instead of kind alone.
    """
    st = dict(rows=0, documents=0, parse_failures=0, no_kind=0, objects=0, in_scope=0,
              in_scope_without_name=0, unnamed_legacy_kinds=0, unnamed_new_classes=0)
    kinds = collections.Counter()
    unmapped = collections.Counter()
    by_class = collections.Counter()
    # For the two checks that can genuinely fail: is a custom resource's defining CRD
    # present, and does a Flux source reference point at a declared source?
    crd_repo, crd_all, crs = collections.defaultdict(set), set(), []
    flux_src, src_refs = set(), []
    probe = KCROGraph()                  # only to reuse _defer_source's reading of the spec
    for repo, content in docs_iter:
        st["rows"] += 1
        try:
            docs = list(yaml.safe_load_all(content))
        except yaml.YAMLError:
            st["parse_failures"] += 1
            continue
        for d in docs:
            st["documents"] += 1
            if not (isinstance(d, dict) and d.get("kind")):
                st["no_kind"] += 1
                continue
            k = str(d["kind"])
            st["objects"] += 1
            kinds[k] += 1
            cls = kcro_class_for(k, d)
            if cls is None:
                unmapped[(api_group(d), k)] += 1
                continue
            st["in_scope"] += 1
            by_class[cls] += 1
            md = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
            spec = d.get("spec") if isinstance(d.get("spec"), dict) else {}
            ns = md.get("namespace") if isinstance(md.get("namespace"), str) else "default"
            if cls == "CustomResourceDefinition":
                names = spec.get("names") if isinstance(spec.get("names"), dict) else {}
                if isinstance(spec.get("group"), str) and isinstance(names.get("kind"), str):
                    crd_repo[repo].add((spec["group"], names["kind"]))
                    crd_all.add((spec["group"], names["kind"]))
            elif cls in CUSTOM_RESOURCE_CLASSES:
                crs.append((repo, api_group(d), k))
            if cls in FLUX_SOURCE_CLASSES and isinstance(md.get("name"), str):
                flux_src.add((repo, ns, k, md["name"]))
            if cls in ("FluxKustomization", "HelmRelease"):
                probe.deferred.clear()
                probe._defer_source(repo, ns, None, spec)
                src_refs += [r[2:] for r in probe.deferred]
            if not ((md or {}).get("name") or (md or {}).get("generateName")):
                # counted in coverage but cannot be individuated: the guard against a
                # change that adds coverage points and almost no graph
                st["in_scope_without_name"] += 1
                # The 17 original kinds keep their v0.3.0 behaviour (one "?" individual
                # per repo/kind/namespace); the classes added since are skipped by the
                # builder. The two are counted apart so this figure reconciles with the
                # builder's own "skipped N new-class objects" line.
                if k in KIND_CLASS and cls == KIND_CLASS[k]:
                    st["unnamed_legacy_kinds"] += 1
                else:
                    st["unnamed_new_classes"] += 1
    st["distinct_kinds"] = len(kinds)
    st["unmapped_kinds_n"] = len(unmapped)
    st["unmapped_objects"] = sum(unmapped.values())
    st["unmapped_kinds"] = [[f"{g}/{k}" if g else k, c] for (g, k), c in unmapped.most_common(20)]
    st["in_scope_by_class"] = dict(sorted(by_class.items()))
    n = len(crs)
    in_repo = sum(1 for repo, g, k in crs if (g, k) in crd_repo[repo])
    in_corpus = sum(1 for _, g, k in crs if (g, k) in crd_all)
    st["custom_resources"] = n
    st["crd_in_same_repo"] = in_repo
    st["crd_in_corpus"] = in_corpus
    st["crd_in_same_repo_rate"] = round(in_repo / n, 4) if n else None
    st["crd_in_corpus_rate"] = round(in_corpus / n, 4) if n else None
    found = len(src_refs)
    resolved = sum(1 for r in src_refs if tuple(r) in flux_src)
    st["source_refs_found"] = found
    st["source_refs_resolved"] = resolved
    st["source_refs_resolved_rate"] = round(resolved / found, 4) if found else None
    st["coverage_pct"] = round(100 * st["in_scope"] / st["objects"], 2) if st["objects"] else None
    return st


def jsonl_contents(path):
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                row = json.loads(line)
                yield (row.get("repo") or row.get("repo_full_name") or "repo",
                       row.get("content") or "")


def arrow_contents(path):
    from datasets import load_from_disk
    for row in load_from_disk(path):
        yield row.get("repo_full_name") or "repo", row["content"] or ""


# ------------------------------------------------------------------ abox side
def tbox_classes():
    g = Graph(); g.parse(TBOX, format="turtle")
    return sorted(str(c).split("#")[-1] for c in g.subjects(RDF.type, OWL.Class)
                  if str(c).startswith(str(KCRO)))


def abox_stats(abox_path):
    """Census + under-mediation, both via KCROGraph so the logic is the shipped one."""
    kg = KCROGraph()                     # reuse the class: under_mediated() reads self.g
    kg.g = Graph()
    fmt = "nt" if str(abox_path).endswith((".nt", ".ntriples")) else "turtle"
    kg.g.parse(abox_path, format=fmt)

    classes = tbox_classes()
    census = {}
    for c in classes:
        n = len(set(kg.g.subjects(RDF.type, KCRO[c])))
        if n:
            census[c] = n
    under = kg.under_mediated()           # the shipped check, not a reimplementation
    per_class, under_by_class = {}, collections.Counter()
    relators = set()
    for c in RELATOR_CLASSES:
        members = set(kg.g.subjects(RDF.type, KCRO[c]))
        if members:
            per_class[c] = len(members)
        if c in ORIGINAL_RELATOR_CLASSES:
            # The headline figures cover the relators of v0.3.0 only. The relators
            # added in v0.5.0 exist only where their target was found, so counting
            # them here would raise the rate without anything having improved.
            relators |= members
    for r in under:
        for t in kg.g.objects(r, RDF.type):
            c = str(t).split("#")[-1]
            if c in RELATOR_CLASSES:
                under_by_class[c] += 1
    total = len(relators)
    # A relator class that is resolved by construction lifts the aggregate rate without
    # improving any pre-existing class, so the per-class split is reported alongside it.
    open_cat = sum(census.get(c, 0) for c in OPEN_CLASSES)
    return {
        "triples": len(kg.g),
        "class_census": dict(sorted(census.items())),
        "empty_classes": [c for c in classes if c not in census],
        "open_category": open_cat,
        "open_category_classes": {c: census.get(c, 0) for c in OPEN_CLASSES},
        "relators_total": total,
        "relators_under_mediated": len(under),
        "resolution_rate": round(1 - len(under) / total, 4) if total else None,
        "relators_by_class": dict(sorted(per_class.items())),
        "new_relators": {c: per_class.get(c, 0) for c in NEW_RELATOR_CLASSES},
        "under_mediated_by_class": dict(sorted(under_by_class.items())),
    }


# --------------------------------------------------------------------- output
def md_row(r):
    def pct(x):
        return "n/a" if x is None else f"{x:.1f}%"
    cov = r["coverage_pct"]
    opn = (100 * r["open_category"] / r["objects"]
           if r["objects"] and r.get("open_category") is not None else None)
    res = 100 * r["resolution_rate"] if r.get("resolution_rate") is not None else None
    pf = 100 * r["parse_failures"] / r["rows"] if r["rows"] else None
    tri = f"{r['triples']:,}" if r.get("triples") else "n/a"
    num = lambda x: "n/a" if x is None else f"{x:,}"
    rate = lambda k: pct(100 * r[k]) if r.get(k) is not None else "n/a"
    src = (f"{r['source_refs_resolved']:,}/{r['source_refs_found']:,}"
           if r.get("source_refs_found") else "none")
    return (f"| {r['corpus']} | {r['objects']:,} | {pct(cov)} | "
            f"{num(r.get('relators_total'))} | {num(r.get('relators_under_mediated'))} | "
            f"{pct(res)} | {num(r.get('custom_resources'))} | {rate('crd_in_same_repo_rate')} | "
            f"{rate('crd_in_corpus_rate')} | {src} | {tri} |")


HEADER = ("| Corpus | Objects | Coverage | Original relators | Unresolved | Resolved | "
          "Custom resources | CRD in repo | CRD in corpus | Source refs resolved | Triples |\n"
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus")
    ap.add_argument("--jsonl"); ap.add_argument("--arrow"); ap.add_argument("--abox")
    ap.add_argument("--out")
    ap.add_argument("--coverage-only", action="store_true",
                    help="skip the ABox pass; coverage depends only on the corpus and "
                         "the dispatch tables, so a stage can be re-measured in minutes")
    ap.add_argument("--table", nargs="*", help="render the combined table from JSON files")
    a = ap.parse_args()

    if a.table:
        rows = [json.loads(Path(f).read_text()) for f in a.table]
        print(HEADER)
        for r in sorted(rows, key=lambda r: -(r["coverage_pct"] or 0)):
            print(md_row(r))
        print()
        nkinds = len(KIND_CLASS) + len(GROUP_KIND_CLASS)
        for r in rows:
            print(f"{r['corpus']}: coverage {r['coverage_pct']}% "
                  f"({r['in_scope']:,}/{r['objects']:,} objects in the {nkinds} mapped kinds)")
        return

    if not a.corpus or not (a.jsonl or a.arrow) or not (a.abox or a.coverage_only):
        ap.error("need --corpus, one of --jsonl / --arrow, and --abox "
                 "(or --coverage-only)")

    src = jsonl_contents(a.jsonl) if a.jsonl else arrow_contents(a.arrow)
    rec = {"corpus": a.corpus,
           "source": a.jsonl or a.arrow,
           "abox": a.abox}
    rec.update(corpus_stats(src))
    if a.coverage_only:
        rec.update(triples=None, class_census={}, empty_classes=[], open_category=None,
                   relators_total=None, relators_under_mediated=None,
                   resolution_rate=None, new_relators={})
    else:
        rec.update(abox_stats(a.abox))

    out = Path(a.out or ROOT / "results" / f"portability-{a.corpus}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=2) + "\n")
    nkinds = len(KIND_CLASS) + len(GROUP_KIND_CLASS)
    print(HEADER); print(md_row(rec))
    print(f"\ncoverage {rec['coverage_pct']}%  "
          f"({rec['in_scope']:,}/{rec['objects']:,} objects in the {nkinds} mapped kinds)")
    if rec["in_scope_without_name"]:
        print(f"  WARNING {rec['in_scope_without_name']:,} in-scope objects have no "
              f"metadata.name or generateName ({rec['unnamed_new_classes']:,} in new "
              f"classes, skipped by the builder; {rec['unnamed_legacy_kinds']:,} in the "
              f"17 original kinds, kept as one '?' individual as in v0.3.0)")
    if not a.coverage_only:
        print(f"open category {rec['open_category_classes']}")
        print(f"original relators: {rec['relators_under_mediated']:,} of "
              f"{rec['relators_total']:,} mediate < 2 "
              f"({100*rec['resolution_rate']:.1f}% resolved); new relators {rec['new_relators']}")
    if rec["custom_resources"]:
        print(f"custom resources {rec['custom_resources']:,}: defining CRD in the same "
              f"repository {rec['crd_in_same_repo']:,} "
              f"({100*rec['crd_in_same_repo_rate']:.1f}%), anywhere in the corpus "
              f"{rec['crd_in_corpus']:,} ({100*rec['crd_in_corpus_rate']:.1f}%)")
    if rec["source_refs_found"]:
        print(f"Flux source references {rec['source_refs_found']:,}: resolved in the same "
              f"repository {rec['source_refs_resolved']:,} "
              f"({100*rec['source_refs_resolved_rate']:.1f}%)")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
