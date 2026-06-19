#!/usr/bin/env python3
"""
make_cq9_view.py — build a focused 3d-force-graph view (like the `/` drilldown)
of the FULL CQ9 result set from the frozen v0.3.0 KCRO ABox.

Not hardcoded to one Pod: it runs CQ9 (indexed joins, so no rdflib blow-up) and
embeds every result path — exposed Service -> ServiceRouting -> Pod -> Container
-> privileged Container — as the graph data in a self-contained HTML page.

Run from the repo root:
    .venv/bin/python figures/make_cq9_view.py
Then open: figures/cq9_view.html   (no backend needed; data is embedded)
"""
import json
from pathlib import Path

from rdflib import Graph, RDFS, RDF, URIRef

ROOT = Path(__file__).resolve().parent.parent
ONT = ROOT / "ontology"
OUT_HTML = ROOT / "figures" / "cq9_view.html"

K = "https://w3id.org/kcro#"
G = "http://purl.org/nemo/gufo#"
def kc(x): return URIRef(K + x)
def gu(x): return URIRef(G + x)
INHERES, MEDIATES, PARTOF = gu("inheresIn"), gu("mediates"), gu("isProperPartOf")

# node type -> gUFO category (drives colour); exposure kept distinct (value-neutral)
GROUP = {
    "Service": "Object", "Pod": "Object", "Container": "Object",
    "ServiceRouting": "Relator",
    "PrivilegedContainer": "Vulnerability",
    "NodePortExposure": "Exposure", "LoadBalancerExposure": "Exposure",
}


def cq9_rows(g):
    def members(cls): return set(g.subjects(RDF.type, kc(cls)))
    nodeport, loadbal = members("NodePortExposure"), members("LoadBalancerExposure")
    routings, pods, privs = members("ServiceRouting"), members("Pod"), members("PrivilegedContainer")
    inheres = {s: o for s, o in g.subject_objects(INHERES)}
    mediates, mediated_by = {}, {}
    for s, o in g.subject_objects(MEDIATES):
        mediates.setdefault(s, set()).add(o)
        mediated_by.setdefault(o, set()).add(s)
    pod_parts = {}
    for s, o in g.subject_objects(PARTOF):
        pod_parts.setdefault(o, set()).add(s)
    bearer_aspects = {}
    for asp, b in inheres.items():
        bearer_aspects.setdefault(b, set()).add(asp)

    rows = set()
    for exp in nodeport | loadbal:
        service = inheres.get(exp)
        if service is None:
            continue
        for sr in mediated_by.get(service, ()):
            if sr not in routings:
                continue
            for pod in mediates.get(sr, ()):
                if pod not in pods:
                    continue
                for container in pod_parts.get(pod, ()):
                    for pv in bearer_aspects.get(container, ()):
                        if pv in privs:
                            rows.add((exp, service, sr, pod, container, pv))
    return rows


def main():
    g = Graph()
    print("loading TBox + v0.3.0 ABox ...")
    g.parse(ONT / "kcro.ttl", format="turtle")
    g.parse(ONT / "kcro-abox.ttl", format="turtle")
    print(f"  {len(g):,} triples")

    rows = cq9_rows(g)
    print(f"CQ9 returned {len(rows)} path(s)")

    type_of = {str(s): str(o).split("#")[-1] for s, o in g.subject_objects(RDF.type)}
    label_of = {str(s): str(o) for s, o in g.subject_objects(RDFS.label)}

    def node(iri):
        i = str(iri)
        t = type_of.get(i, i.split("#")[-1])
        lbl = label_of.get(i)
        name = lbl.split("/", 1)[1] if (lbl and "/" in lbl) else (lbl or "")
        return {"id": i, "group": GROUP.get(t, "Object"),
                "label": f"{t}: {name}" if name else t}

    nodes, links, seen = {}, [], set()

    def add_edge(s, o, pred):
        for n in (s, o):
            if str(n) not in nodes:
                nodes[str(n)] = node(n)
        key = (str(s), str(o), pred)
        if key not in seen:
            seen.add(key)
            links.append({"source": str(s), "target": str(o), "label": pred})

    for exp, service, sr, pod, container, pv in rows:
        add_edge(exp, service, "inheresIn")
        add_edge(sr, service, "mediates")
        add_edge(sr, pod, "mediates")
        add_edge(container, pod, "isProperPartOf")
        add_edge(pv, container, "inheresIn")

    data = {"nodes": list(nodes.values()), "links": links}
    print(f"subgraph: {len(data['nodes'])} nodes, {len(data['links'])} edges")

    OUT_HTML.write_text(HTML.replace("/*DATA*/", json.dumps(data, indent=2)))
    print(f"wrote {OUT_HTML.relative_to(ROOT)}  (open it in a browser)")


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>KCRO — CQ9 result paths (v0.3.0)</title>
  <script src="https://unpkg.com/three-spritetext"></script>
  <script src="https://unpkg.com/3d-force-graph"></script>
  <style>
    body { margin:0; background:#ffffff; overflow:hidden; font-family:monospace; color:#1a2230; }
    #graph-container { width:100vw; height:100vh; }
    .panel { position:absolute; z-index:10; background:rgba(255,255,255,0.92);
             border:1px solid #cbd5e0; border-radius:6px; padding:12px 14px; }
    #info { top:16px; left:16px; max-width:420px; }
    #legend { bottom:16px; left:16px; }
    h1 { margin:0 0 4px; font-size:1.05rem; color:#2b6cb0; }
    .sub { margin:0; font-size:0.8rem; color:#566069; }
    .row { font-size:0.8rem; margin:3px 0; }
    .dot { display:inline-block; width:11px; height:11px; border-radius:50%; margin-right:7px; vertical-align:middle; }
  </style>
</head>
<body>
  <div id="info" class="panel">
    <h1>CQ9 — externally exposed → privileged container</h1>
    <p class="sub">Every CQ9 result path over the frozen <b>v0.3.0</b> ABox
       (537,947 triples). Hover a node for its IRI; drag to rotate, scroll to zoom.</p>
  </div>
  <div id="legend" class="panel">
    <div class="row"><span class="dot" style="background:#2c7a7b"></span>Exposure (NodePort/LoadBalancer)</div>
    <div class="row"><span class="dot" style="background:#2b6cb0"></span>Object (Service / Pod / Container)</div>
    <div class="row"><span class="dot" style="background:#6b46c1"></span>Relator (ServiceRouting)</div>
    <div class="row"><span class="dot" style="background:#c53030"></span>Vulnerability (PrivilegedContainer)</div>
  </div>
  <div id="graph-container"></div>
  <script>
    const graphData = /*DATA*/;
    const colorMap = { Object:"#2b6cb0", Relator:"#6b46c1", Vulnerability:"#c53030", Exposure:"#2c7a7b" };
    const Graph = ForceGraph3D()(document.getElementById('graph-container'))
      .graphData(graphData)
      .backgroundColor('#ffffff')
      .nodeLabel('id')
      .nodeColor(n => colorMap[n.group] || '#333333')
      .nodeRelSize(5)
      .nodeThreeObjectExtend(true)
      .nodeThreeObject(n => {
        if (typeof SpriteText === 'undefined') return false;
        const s = new SpriteText(n.label);
        s.color = colorMap[n.group] || '#ffffff'; s.textHeight = 3;
        return s;
      })
      .linkLabel('label')
      .linkColor(() => '#8a94a6')
      .linkOpacity(0.85)
      .linkWidth(1.6)
      .linkDirectionalArrowLength(3)
      .linkDirectionalArrowRelPos(1)
      .linkDirectionalParticles(2)
      .linkDirectionalParticleSpeed(0.006)
      .onNodeClick(n => {
        const d = 60, r = 1 + d / Math.hypot(n.x, n.y, n.z);
        Graph.cameraPosition({ x: n.x*r, y: n.y*r, z: n.z*r }, n, 1500);
      });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
