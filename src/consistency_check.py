#!/usr/bin/env python3
"""
consistency_check.py --- run HermiT over the KCRO TBox plus one corpus ABox.

    python src/consistency_check.py --abox ontology/kcro-abox-rahman.ttl --corpus rahman

A portability claim in its own right: the ontology should stay consistent under
data it was not designed around. Records the verdict and the wall-clock time.

HermiT comes from the owlready2 wheel. owlready2 cannot read Turtle, so the TBox
and ABox are merged with rdflib and written as N-Triples first; that merged file
is kept so a reviewer can re-run the reasoner independently.
"""
import argparse, json, subprocess, threading, time, sys
from pathlib import Path

from rdflib import Graph, OWL, RDFS, Literal, URIRef, XSD

ROOT = Path(__file__).resolve().parent.parent
TBOX = ROOT / "ontology" / "kcro.ttl"
GUFO_URL = "https://nemo-ufes.github.io/gufo/gufo.ttl"
GUFO_CACHE = ROOT / "results" / "gufo.ttl"      # cached so runs are reproducible offline


def load_gufo():
    """KCRO owl:imports gUFO, which is Turtle; owlready2 reads only n-triples/RDF-XML,
    so gUFO is inlined here and the import statement dropped."""
    if not GUFO_CACHE.exists():
        g = Graph(); g.parse(GUFO_URL, format="turtle")
        GUFO_CACHE.parent.mkdir(parents=True, exist_ok=True)
        g.serialize(destination=str(GUFO_CACHE), format="turtle")
    g = Graph(); g.parse(GUFO_CACHE, format="turtle")
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--abox", required=True)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep", action="store_true", help="keep the merged n-triples file")
    ap.add_argument("--java-memory", type=int, default=24000,
                    help="HermiT heap in MB (owlready2 defaults to a 2 GB heap, which "
                         "is far too small for a 680k-triple ABox)")
    ap.add_argument("--timeout", type=int, default=0,
                    help="seconds before HermiT is stopped and recorded as a timeout")
    a = ap.parse_args()

    merged = ROOT / "results" / f"merged-{a.corpus}.nt"
    merged.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    g = Graph()
    g.parse(TBOX, format="turtle")
    gufo = load_gufo()
    for t in gufo:
        g.add(t)
    tbox_n_raw = len(g)
    fmt = "nt" if str(a.abox).endswith((".nt", ".ntriples")) else "turtle"
    g.parse(a.abox, format=fmt)
    abox_n = len(g) - tbox_n_raw
    # Stripping happens AFTER both parses: the ABox carries its own
    # owl:imports <https://w3id.org/kcro> header, and leaving it in makes
    # owlready2 resolve that IRI over the network (it redirects to the GitHub
    # repo, which is not RDF/XML) instead of using the TBox already inlined here.
    for t in list(g.triples((None, OWL.imports, None))):
        g.remove(t)                              # everything is inlined now
    # owlready2 chases rdfs:seeAlso as if it were an ontology reference (the TBox
    # points at the GitHub repo). It is an annotation with no bearing on DL
    # consistency, so drop it from the reasoning copy. kcro.ttl is not modified.
    for t in list(g.triples((None, RDFS.seeAlso, None))):
        g.remove(t)
    # HermiT implements exactly the OWL 2 datatype map, which has xsd:dateTime and
    # xsd:dateTimeStamp but NOT xsd:date, so anything typed xsd:date makes it refuse
    # the whole ontology. Three of the four uses here are ontology metadata
    # (dcterms:issued/modified on KCRO, dct:created/modified on gUFO); the fourth is
    # gUFO's own gufo:hasBeginPointInXSDDate rdfs:range xsd:date, a property KCRO
    # never uses. None of them bear on whether the KCRO ABox is consistent, so they
    # are dropped from the reasoning copy and recorded below.
    removed = []
    for s_, p_, o_ in list(g):
        if (isinstance(o_, Literal) and o_.datatype == XSD.date) or \
           (p_ == RDFS.range and o_ == XSD.date):
            g.remove((s_, p_, o_)); removed.append(f"{s_} {p_} {o_}")
    tbox_n = len(g) - abox_n
    g.serialize(destination=str(merged), format="nt")
    t_merge = time.time() - t0
    print(f"merged {tbox_n:,} TBox+gUFO + {len(g)-tbox_n:,} ABox = {len(g):,} triples "
          f"in {t_merge:.1f}s -> {merged.name}", flush=True)

    import owlready2
    import owlready2.reasoning
    from owlready2 import get_ontology, sync_reasoner_hermit, World
    from owlready2.base import OwlReadyInconsistentOntologyError

    owlready2.reasoning.JAVA_MEMORY = a.java_memory
    print(f"HermiT heap: {a.java_memory} MB"
          + (f"  timeout: {a.timeout}s" if a.timeout else ""), flush=True)

    timed_out = threading.Event()
    if a.timeout:
        def stop():
            timed_out.set()
            subprocess.run(["pkill", "-f", "org.semanticweb.HermiT"],
                           capture_output=True)
        watchdog = threading.Timer(a.timeout, stop)
        watchdog.daemon = True
        watchdog.start()

    t1 = time.time()
    verdict, detail, unsat = "unknown", "", None
    try:
        w = World()
        onto = w.get_ontology(merged.as_uri()).load()
        sync_reasoner_hermit(w, infer_property_values=False, debug=0)
        verdict = "consistent"
        # A consistent ontology can still contain classes that can never have a
        # member. With no individuals, consistency alone is close to vacuous, so
        # this is the check that actually exercises the TBox axioms.
        unsat = sorted(str(c) for c in w.inconsistent_classes())
    except OwlReadyInconsistentOntologyError as e:
        verdict, detail = "INCONSISTENT", str(e)[:400]
    except Exception as e:                       # reasoner crash / OOM / timeout
        if timed_out.is_set():
            verdict = "timeout"
            detail = f"HermiT did not terminate within {a.timeout}s at {a.java_memory} MB heap"
        else:
            verdict, detail = "error", f"{type(e).__name__}: {str(e)[:400]}"
    wall = time.time() - t1
    if a.timeout:
        watchdog.cancel()

    rec = {"corpus": a.corpus, "abox": a.abox, "verdict": verdict,
           "removed_for_reasoning": removed,
           "unsatisfiable_classes": unsat,
           "detail": detail, "reasoner": f"HermiT via owlready2 {owlready2.VERSION}",
           "triples_total": len(g), "triples_tbox": tbox_n,
           "triples_abox": len(g) - tbox_n,
           "merge_seconds": round(t_merge, 1), "reasoner_seconds": round(wall, 1),
           "java_memory_mb": a.java_memory, "timeout_seconds": a.timeout or None}
    out = Path(a.out or ROOT / "results" / f"consistency-{a.corpus}.json")
    out.write_text(json.dumps(rec, indent=2) + "\n")
    print(f"{a.corpus:22} {verdict:14} {wall:8.1f}s reasoning -> {out.name}")
    if detail:
        print("   ", detail[:200])
    if unsat is not None:
        print(f"    unsatisfiable classes: {len(unsat)}" + (f" {unsat[:5]}" if unsat else ""))
    if not a.keep:
        merged.unlink(missing_ok=True)
    return 0 if verdict == "consistent" else 1


if __name__ == "__main__":
    sys.exit(main())
