#!/usr/bin/env python3
"""
make_cq9_figure.py — render ONE CQ9 attack path from the v0.3.0 KCRO ABox as a
publication-quality graphviz figure (figures/cq9-path.{pdf,png}).

CQ9 = "which privileged containers are reachable from outside the cluster via an
externally exposed Service?" The figure is built directly from the data with
graphviz (dot) — NOT a screenshot of the GPU explorer.

Data source: the frozen v0.3.0 thesis snapshot (ontology/kcro-abox.ttl) + the
TBox (ontology/kcro.ttl), loaded into rdflib. (Not the v0.4.0 provenance graph.)

Run from the repo root:
    .venv/bin/python figures/make_cq9_figure.py

Outputs: figures/cq9-path.pdf (vector) and figures/cq9-path.png (300 dpi).
"""
import re
from pathlib import Path

from rdflib import Graph, RDFS, RDF, URIRef
from graphviz import Digraph

ROOT = Path(__file__).resolve().parent.parent
ONT = ROOT / "ontology"
OUT = ROOT / "figures" / "cq9-path"     # graphviz appends .pdf / .png

K = "https://w3id.org/kcro#"
G = "http://purl.org/nemo/gufo#"
def kc(x): return URIRef(K + x)
def gu(x): return URIRef(G + x)
INHERES, MEDIATES, PARTOF = gu("inheresIn"), gu("mediates"), gu("isProperPartOf")

# CQ9 — externally exposed Service routing to a Pod with a privileged container.
# This SPARQL is the *definition* of the bindings drawn below. We evaluate it with
# indexed joins (cq9_rows) rather than g.query(), because rdflib's SPARQL planner
# blows up on this multi-hop pattern over the 537k-triple ABox (same reason
# cq_runner.py exists); the indexed result is identical to the query's.
QUERY = """
PREFIX kcro: <https://w3id.org/kcro#>
PREFIX gufo: <http://purl.org/nemo/gufo#>
SELECT ?exp ?lvl ?service ?sr ?pod ?container ?pv WHERE {
  ?exp gufo:inheresIn ?service ; a ?lvl .
  VALUES ?lvl { kcro:NodePortExposure kcro:LoadBalancerExposure }
  ?sr a kcro:ServiceRouting ; gufo:mediates ?service, ?pod .
  ?pod a kcro:Pod .
  ?container gufo:isProperPartOf ?pod .
  ?pv a kcro:PrivilegedContainer ; gufo:inheresIn ?container .
}
"""

VARS = ["exp", "lvl", "service", "sr", "pod", "container", "pv"]

# Orientation. "TB" (top->bottom) fits a single ~8.5 cm column with readable text;
# a 6-node "LR" row is ~12 in wide and shrinks to ~3 pt at one column — use "LR"
# only if you place the figure across both columns. The attack flow is preserved
# either way (exposed Service first, privileged Container last).
ORIENT = "TB"


def localname(iri):
    return str(iri).split("#")[-1].split("/")[-1]


def strip_hash(s):
    return re.sub(r"-[0-9a-f]{12,}$", "", s)


def readable_name(graph, iri):
    """rdfs:label ('Kind/name' -> 'name') if present, else cleaned local name."""
    lbl = graph.value(iri, RDFS.label)
    if lbl:
        s = str(lbl)
        return s.split("/", 1)[1] if "/" in s else s
    return strip_hash(localname(iri))


def node_label(kind, name):
    return f"{kind}\n{name}" if name and name != kind else kind


def cq9_rows(g):
    """The CQ9 bindings, computed with indexed joins (equivalent to QUERY)."""
    def members(cls):
        return set(g.subjects(RDF.type, kc(cls)))
    nodeport, loadbal = members("NodePortExposure"), members("LoadBalancerExposure")
    routings, pods, privs = members("ServiceRouting"), members("Pod"), members("PrivilegedContainer")

    inheres = {s: o for s, o in g.subject_objects(INHERES)}      # aspect -> bearer
    mediated_by, mediates = {}, {}                                # entity<->relator
    for s, o in g.subject_objects(MEDIATES):
        mediates.setdefault(s, set()).add(o)
        mediated_by.setdefault(o, set()).add(s)
    pod_parts = {}                                                # pod -> {containers}
    for s, o in g.subject_objects(PARTOF):
        pod_parts.setdefault(o, set()).add(s)
    bearer_aspects = {}                                           # bearer -> {aspects}
    for asp, b in inheres.items():
        bearer_aspects.setdefault(b, set()).add(asp)

    rows = set()
    for exp in nodeport | loadbal:
        service = inheres.get(exp)
        if service is None:
            continue
        lvl = kc("NodePortExposure") if exp in nodeport else kc("LoadBalancerExposure")
        for sr in mediated_by.get(service, ()):
            if sr not in routings:
                continue
            for pod in mediates.get(sr, ()):
                if pod not in pods:
                    continue
                for container in pod_parts.get(pod, ()):
                    for pv in bearer_aspects.get(container, ()):
                        if pv in privs:
                            rows.add((exp, lvl, service, sr, pod, container, pv))
    return [dict(zip(VARS, t)) for t in rows]


def main():
    g = Graph()
    print("loading TBox + v0.3.0 ABox ...")
    g.parse(ONT / "kcro.ttl", format="turtle")
    g.parse(ONT / "kcro-abox.ttl", format="turtle")
    print(f"  {len(g):,} triples loaded")

    rows = cq9_rows(g)
    print(f"CQ9 returned {len(rows)} row(s)"
          + ("" if len(rows) == 6 else "  (expected 6 — check the ABox version!)"))

    # Deterministic pick: sort by the ?service IRI, take the first.
    rows.sort(key=lambda row: str(row["service"]))
    vals = rows[0]

    print("\nChosen path (sort-by-?service, first row) — verify / cite these IRIs:")
    for v in VARS:
        print(f"  ?{v:9} = {vals[v]}")

    lvl_type = localname(vals["lvl"])                       # NodePort/LoadBalancer
    svc_name = readable_name(g, vals["service"])
    pod_name = readable_name(g, vals["pod"])
    print(f"\nFigure: {lvl_type} -> Service '{svc_name}' -> ServiceRouting -> "
          f"Pod '{pod_name}' -> Container -> PrivilegedContainer")

    # ---- graphviz: left-to-right attack path -----------------------------
    # Colour by gUFO category + distinct shape (legible in greyscale):
    OBJECT   = dict(fillcolor="#d6e4ff", color="#2b6cb0", shape="box")       # blue
    RELATOR  = dict(fillcolor="#e9d8fd", color="#6b46c1", shape="diamond")   # purple
    VULN     = dict(fillcolor="#ffd6d6", color="#c53030", shape="octagon")   # red
    EXPOSURE = dict(fillcolor="#cdeeea", color="#2c7a7b", shape="ellipse")   # teal

    dot = Digraph("cq9")
    dot.attr(rankdir=ORIENT, bgcolor="white", margin="0",
             nodesep="0.22", ranksep="0.40")
    dot.attr("node", style="filled", fontname="Helvetica", fontsize="11",
             penwidth="1.5", fontcolor="#11181c", margin="0.07,0.045")
    dot.attr("edge", fontname="Helvetica", fontsize="9",
             color="#566069", fontcolor="#566069", arrowsize="0.7")

    dot.node("exp", lvl_type, **EXPOSURE)
    dot.node("service", node_label("Service", svc_name), **OBJECT)
    dot.node("sr", "ServiceRouting", **RELATOR)
    dot.node("pod", node_label("Pod", pod_name), **OBJECT)
    dot.node("container", "Container", **OBJECT)
    dot.node("pv", "PrivilegedContainer", **VULN)

    # Invisible spine keeps the left→right attack order (exposure … privileged).
    for a, b in [("service", "sr"), ("pod", "container"), ("container", "pv")]:
        dot.edge(a, b, style="invis")

    # Real gUFO triples, labelled by predicate (true direction preserved).
    dot.edge("exp", "service", label="inheresIn")                       # orders exp<service
    dot.edge("sr", "service", label="mediates", constraint="false")
    dot.edge("sr", "pod", label="mediates")                             # orders sr<pod
    dot.edge("container", "pod", label="isProperPartOf", constraint="false")
    dot.edge("pv", "container", label="inheresIn", constraint="false")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    pdf = dot.render(str(OUT), format="pdf", cleanup=True)
    dot.attr(dpi="300")
    png = dot.render(str(OUT), format="png", cleanup=True)
    print(f"\nwrote {Path(pdf).relative_to(ROOT)} and {Path(png).relative_to(ROOT)}")


if __name__ == "__main__":
    main()
