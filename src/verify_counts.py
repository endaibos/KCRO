#!/usr/bin/env python3
"""
Reuses srq3's ANALYSIS_KEYS + flatten + the same loose-matching the --verify diff
uses, but computes it over the EXISTING ontology/kcro-abox.ttl and
results/security_analysis.json — it never rebuilds or writes anything.

Run from the repo root:
    .venv/bin/python src/verify_counts.py
"""
import json
from pathlib import Path

from rdflib import Graph, RDF
import srq3

ROOT = Path(__file__).resolve().parent.parent
ABOX = ROOT / "ontology" / "kcro-abox.ttl"
ANALYSIS = ROOT / "results" / "security_analysis.json"
K = "https://w3id.org/kcro#"


def main():
    g = Graph()
    g.parse(ABOX, format="turtle")
    n = len(g)
    print(f"# ABox: {ABOX}  ({n:,} triples"
          + (" — v0.3.0 ✓)" if n == 537947 else " — NOT 537,947, check the version!)"))

    # KG: distinct individuals per KCRO class
    kg = {}
    for _s, _p, o in g.triples((None, RDF.type, None)):
        o = str(o)
        if o.startswith(K):
            cls = o.split("#")[1]
            kg[cls] = kg.get(cls, 0) + 1

    # Survey occurrences (flattened, matched exactly as srq3 does)
    flat = srq3.flatten(json.loads(ANALYSIS.read_text()))

    def survey_count(candidates):
        for cand in candidates:
            hits = [v for k, v in flat.items()
                    if k == cand or k.endswith("." + cand) or cand in k]
            if hits:
                return int(hits[0])
        return None

    rows = []
    for cls, cands in srq3.ANALYSIS_KEYS.items():
        rows.append((cls, survey_count(cands), kg.get(cls, 0)))

    # order by survey count descending (None last), then name
    rows.sort(key=lambda r: (-(r[1] if r[1] is not None else -1), r[0]))

    # (a) raw table
    print("\n## (a) raw counts (ordered by survey desc)\n")
    print(f"{'class':30} {'survey':>10} {'KG':>10}")
    print("-" * 52)
    tot_s = tot_k = 0
    flagged = []
    for cls, s, k in rows:
        print(f"{cls:30} {('—' if s is None else f'{s:,}'):>10} {k:>10,}")
        tot_s += s or 0
        tot_k += k
        if s is not None and k > s:
            flagged.append((cls, s, k))
    print("-" * 52)
    print(f"{'TOTAL':30} {tot_s:>10,} {tot_k:>10,}")

    # (b) LaTeX rows
    print("\n## (b) LaTeX rows (class & survey & KG)\n")
    for cls, s, k in rows:
        print(f"{cls} & {('--' if s is None else f'{s}')} & {k} \\\\")

    print("\n## KG > survey (should be empty)")
    if flagged:
        for cls, s, k in flagged:
            print(f"  !! {cls}: KG {k:,} > survey {s:,}  (BUG)")
    else:
        print("  none — every KG count <= survey occurrences ✓")


if __name__ == "__main__":
    main()
