"""
kcro_server.py --- a tiny local SPARQL + static-file server for the KCRO abox.

Loads ``kcro-abox.ttl`` into rdflib once at startup and then serves three things
on the same port (so the browser's fetch() calls are same-origin --- no CORS):

    GET  /                -> hero_visualizer.html
    GET  /pods            -> JSON list of the most heavily-connected Pods
                             (?min=<int>&limit=<int>), used to populate the picker
    POST /graph           -> body {"sparql": "<CONSTRUCT ...>"}; runs the query and
                             returns force-graph JSON {nodes, links}, classifying
                             nodes by the gUFO role they play in the edges
    POST /sparql          -> body {"sparql": "<any query>"}; returns rdflib's native
                             SPARQL JSON results (escape hatch for arbitrary SELECTs)

Run::

    .venv/bin/python kcro_server.py            # then open http://localhost:8000

No dependencies beyond rdflib (already in requirements via the extract script).
The one-time turtle parse of the 26 MB abox takes ~30-60 s; the server stays up
afterwards, so you only pay it once per launch.
"""

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from collections import defaultdict

from rdflib import RDF, RDFS, Graph, Namespace, URIRef

HERE = Path(__file__).resolve().parent
ABOX = HERE / "kcro-abox.ttl"
HTML = HERE / "hero_visualizer.html"
PORT = 8000

# Extra static files served by path -> (filesystem name, content-type).
STATIC = {
    "/full2d": ("full_2d_visualizer.html", "text/html; charset=utf-8"),
    "/full3d": ("full_3d_visualizer.html", "text/html; charset=utf-8"),
    "/full_positions.bin": ("full_positions.bin", "application/octet-stream"),
    "/full_positions_2d.bin": ("full_positions_2d.bin", "application/octet-stream"),
}

GUFO = Namespace("http://purl.org/nemo/gufo#")
KCRO = Namespace("https://w3id.org/kcro#")
DATA = Namespace("https://w3id.org/kcro/data#")  # the ':' prefix in the abox
PROV = Namespace("http://www.w3.org/ns/prov#")   # provenance (v0.4.0)

# Predicates that produce a visible edge, and the gUFO role of their *subject*.
# Classification priority (highest first): a node that is ever the subject of
# inheresIn is a Vulnerability; subject of mediates is a Relator; anything that
# only ever appears as an edge endpoint is an Object.
INHERES_IN = str(GUFO.inheresIn)
MEDIATES = str(GUFO.mediates)

# Shared prefix preamble so user-written queries can stay terse.
PREFIXES = """
PREFIX kcro: <https://w3id.org/kcro#>
PREFIX gufo: <http://purl.org/nemo/gufo#>
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""

# Most-connected-Pods query, used by /pods to populate the dropdown.
PODS_QUERY = PREFIXES + """
SELECT ?pod (COUNT(?x) AS ?deg) WHERE {
  ?pod a kcro:Pod .
  ?x ?p ?pod .
  FILTER(?p IN (gufo:inheresIn, gufo:mediates))
} GROUP BY ?pod ORDER BY DESC(?deg)
"""

# Predicates that produce a visible edge in the class-level overview.
ISPROPERPARTOF = str(GUFO.isProperPartOf)
OVERVIEW_PREDICATES = (INHERES_IN, MEDIATES, ISPROPERPARTOF)

graph = Graph()
overview_cache = None  # built once at startup; the abox is static
full_cache = None      # full instance graph, also built once at startup


def localname(iri: str) -> str:
    return iri.split("#")[-1].split("/")[-1]


def construct_to_forcegraph(result_graph) -> dict:
    """Turn a CONSTRUCTed rdflib graph into {nodes, links} for 3d-force-graph.

    Nodes are classified by the role they play across all edges in the result:
    inheresIn-subject -> Vulnerability, mediates-subject -> Relator, else Object.
    """
    triples = [(str(s), str(p), str(o)) for s, p, o in result_graph]

    vulnerabilities, relators = set(), set()
    for s, p, _o in triples:
        if p == INHERES_IN:
            vulnerabilities.add(s)
        elif p == MEDIATES:
            relators.add(s)

    def group_of(node: str) -> str:
        if node in vulnerabilities:
            return "Vulnerability"
        if node in relators:
            return "Relator"
        return "Object"

    nodes, links = {}, []
    for s, p, o in triples:
        for node in (s, o):
            if node not in nodes:
                nodes[node] = {"id": node, "group": group_of(node),
                               "label": localname(node)}
        links.append({"source": s, "target": o, "label": localname(p)})

    return {"nodes": list(nodes.values()), "links": links}


def build_overview() -> dict:
    """Class-level summary of the whole abox: nodes are kcro classes (sized by
    instance count), links are predicate aggregations (weighted by edge count).

    Computed by iterating rdflib's triple indexes directly rather than via SPARQL:
    the equivalent ``SELECT ... GROUP BY`` over 530k triples takes minutes in
    rdflib's query engine, whereas this runs in a couple of seconds. The UI shows
    the equivalent SPARQL for transparency. Cached at startup since the abox is
    static.
    """
    # 1. Map every individual to the localname of its rdf:type, and count classes.
    type_of, counts = {}, defaultdict(int)
    for s, t in graph.subject_objects(RDF.type):
        cls = localname(str(t))
        type_of[str(s)] = cls
        counts[cls] += 1

    # 2. Aggregate instance edges onto (subjectClass, predicate, objectClass).
    agg = defaultdict(int)
    for pred in OVERVIEW_PREDICATES:
        for s, o in graph.subject_objects(URIRef(pred)):
            st, ot = type_of.get(str(s)), type_of.get(str(o))
            if st and ot:
                agg[(st, localname(pred), ot)] += 1

    # 3. Classify each class by the gUFO role it plays (subject of inheresIn ->
    #    Vulnerability, of mediates -> Relator, else Object).
    group = {}
    for (st, plabel, _ot) in agg:
        if plabel == "inheresIn":
            group[st] = "Vulnerability"
        elif plabel == "mediates":
            group.setdefault(st, "Relator")

    nodes, links = {}, []
    for (st, plabel, ot), w in agg.items():
        for cls in (st, ot):
            if cls not in nodes:
                nodes[cls] = {"id": cls, "group": group.get(cls, "Object"),
                              "label": f"{cls} ({counts.get(cls, 0):,})",
                              "count": counts.get(cls, 0)}
        links.append({"source": st, "target": ot, "label": plabel, "weight": w})

    return {"nodes": list(nodes.values()), "links": links}


def build_full() -> dict:
    """The ENTIRE instance graph in a compact, integer-indexed form for GPU
    renderers (Cosmos 2D / Three.js point cloud): ~208k nodes, ~196k edges.

    Returns parallel arrays rather than objects to keep the payload small:
      groups   -- one int per node (index == id): 0 Object, 1 Relator, 2 Vulnerability
      links    -- flat [src0, tgt0, src1, tgt1, ...] integer index pairs
      ids      -- node IRI localname per index (the /node lookup key)
      names    -- human label per index: rdfs:label ("Pod/grafana") if present,
                  else the class ("ServiceRouting"), else the localname
      clusters -- connected-component id per index
      repoId   -- index into `repos` (source repository) per node, propagated
                  across each component; -1 if unknown
      nsId     -- index into `namespaces` (Kubernetes namespace) per node; -1 if unknown
      repos / namespaces -- the string tables repoId / nsId index into
    Group is assigned by gUFO role: subject of inheresIn -> Vulnerability(2),
    of mediates -> Relator(1), otherwise Object(0). Cached at startup.
    """
    # Pre-index naming + provenance material in a few fast index scans.
    label_of = {str(s): str(o) for s, o in graph.subject_objects(RDFS.label)}
    type_of = {str(s): localname(str(o)) for s, o in graph.subject_objects(RDF.type)}
    # object/container -> its repo's human label (via prov:wasDerivedFrom)
    repo_of = {str(s): label_of.get(str(o), localname(str(o)))
               for s, o in graph.subject_objects(PROV.wasDerivedFrom)}
    ns_of = {str(s): str(o) for s, o in graph.subject_objects(KCRO.namespace)}

    # Repository and the ontology header are provenance/metadata, not graph nodes.
    SKIP_TYPES = {"Repository", "Ontology"}

    idx = {}            # IRI -> integer index
    ids = []            # index -> localname (lookup key)
    names = []          # index -> human label
    groups = []         # index -> group code

    def node_id(iri: str) -> int:
        i = idx.get(iri)
        if i is None:
            i = idx[iri] = len(ids)
            ids.append(localname(iri))
            # readable name: real K8s label > class name > raw localname
            names.append(label_of.get(iri) or type_of.get(iri) or localname(iri))
            groups.append(0)
        return i

    # Every typed individual is a node, except provenance/header metadata.
    for s in graph.subjects(RDF.type):
        siri = str(s)
        if type_of.get(siri) not in SKIP_TYPES:
            node_id(siri)

    links = []
    # code = the group a *subject* of this predicate should have (max wins).
    for pred, code in ((INHERES_IN, 2), (MEDIATES, 1), (ISPROPERPARTOF, 0)):
        for s, o in graph.subject_objects(URIRef(pred)):
            si, oi = node_id(str(s)), node_id(str(o))
            links.append(si)
            links.append(oi)
            if code > groups[si]:
                groups[si] = code

    clusters = connected_component_ids(len(ids), links)

    # Propagate repo/namespace across each component: aspects/relators inherit
    # the repo of the object they hang off (every component is single-repo, since
    # cross-repo references never resolve in the pipeline).
    iri_of = [None] * len(ids)
    for iri, i in idx.items():
        iri_of[i] = iri
    comp_repo, comp_ns = {}, {}
    for i, iri in enumerate(iri_of):
        c = clusters[i]
        if iri in repo_of:
            comp_repo.setdefault(c, repo_of[iri])
        if iri in ns_of:
            comp_ns.setdefault(c, ns_of[iri])

    repos, repo_index = [], {}
    namespaces, ns_index = [], {}

    def intern(table, index, value):
        if value is None:
            return -1
        j = index.get(value)
        if j is None:
            j = index[value] = len(table)
            table.append(value)
        return j

    repoId, nsId = [], []
    for i, iri in enumerate(iri_of):
        r = repo_of.get(iri) or comp_repo.get(clusters[i])
        ns = ns_of.get(iri) or comp_ns.get(clusters[i])
        repoId.append(intern(repos, repo_index, r))
        nsId.append(intern(namespaces, ns_index, ns))

    return {"count": len(ids), "groups": groups, "links": links,
            "ids": ids, "names": names, "clusters": clusters,
            "repoId": repoId, "nsId": nsId,
            "repos": repos, "namespaces": namespaces}


def connected_component_ids(n: int, links: list) -> list:
    """One integer per node naming its connected component (scipy union-find)."""
    if not links:
        return list(range(n))
    import numpy as np
    import scipy.sparse as sp
    import scipy.sparse.csgraph as csgraph
    arr = np.asarray(links, dtype=np.int64).reshape(-1, 2)
    m = sp.coo_matrix((np.ones(len(arr)), (arr[:, 0], arr[:, 1])), shape=(n, n))
    _, labels = csgraph.connected_components(m, directed=False)
    return labels.tolist()


def node_details(name: str, limit: int = 60) -> dict:
    """Everything the abox knows about one individual, by its localname:
    its rdf:type(s), datasetKey annotation, and its outgoing / incoming edges.
    """
    iri = URIRef(str(DATA) + name)
    types = [localname(str(t)) for t in graph.objects(iri, RDF.type)]

    dataset_key = None
    out = []
    for p, o in graph.predicate_objects(iri):
        ps = str(p)
        if ps == str(RDF.type):
            continue
        if ps == str(KCRO.datasetKey):
            dataset_key = str(o)
            continue
        out.append({"p": localname(ps),
                    "target": localname(str(o)) if isinstance(o, URIRef) else str(o)})

    inc = [{"source": localname(str(s)), "p": localname(str(p))}
           for s, p in graph.subject_predicates(iri)]

    return {"id": name, "iri": str(iri), "types": types, "datasetKey": dataset_key,
            "out": out[:limit], "in": inc[:limit],
            "outCount": len(out), "inCount": len(inc)}


# ---------------------------------------------------------------- SPARQL panel
QUERY_LOCK = threading.Lock()            # rdflib's query path is not thread-safe
ROW_CAP = 500                            # hard cap, mirrors the UI's LIMIT 500
FORBIDDEN = re.compile(r"\b(INSERT|DELETE|DROP|CLEAR|LOAD|CREATE|MOVE|COPY|ADD)\b", re.I)


def run_sparql(query: str) -> dict:
    """Execute a read-only SELECT/ASK and return {vars, rows} for the SPARQL panel.
    IRIs come back as full strings; the client reduces them to localnames."""
    if FORBIDDEN.search(query):
        return {"error": "read-only endpoint: SELECT/ASK only"}
    if not query.lstrip().upper().startswith("PREFIX"):
        query = PREFIXES + query
    try:
        with QUERY_LOCK:
            res = graph.query(query)
    except Exception as exc:
        return {"error": f"query error: {exc}"}
    if res.type == "ASK":
        return {"vars": ["ask"], "rows": [[bool(res.askAnswer)]]}
    vars_ = [str(v) for v in (res.vars or [])]
    rows = []
    for binding in res:
        rows.append([None if v is None else str(v) for v in binding])
        if len(rows) >= ROW_CAP:
            break
    return {"vars": vars_, "rows": rows}


# ---------------------------------------------------------------- layout store
POS_FILE = HERE / "layout_positions.json"


def save_positions(payload: dict) -> dict:
    pos = payload.get("positions")
    if not isinstance(pos, list) or not pos:
        return {"error": "positions: non-empty list expected"}
    POS_FILE.write_text(json.dumps({"positions": pos}))
    return {"saved": len(pos) // 2}


def load_positions():
    if POS_FILE.exists():
        return json.loads(POS_FILE.read_text())
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, code, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, HTML.read_text(encoding="utf-8"),
                       content_type="text/html; charset=utf-8")
        elif path == "/pods":
            params = dict(p.split("=", 1) for p in
                          (self.path.split("?", 1)[1].split("&")
                           if "?" in self.path else []) if "=" in p)
            min_deg = int(params.get("min", 15))
            limit = int(params.get("limit", 50))
            rows = []
            for row in graph.query(PODS_QUERY):
                deg = int(row.deg)
                if deg < min_deg:
                    break  # ordered DESC, so we can stop
                rows.append({"id": str(row.pod), "label": localname(str(row.pod)),
                             "degree": deg})
                if len(rows) >= limit:
                    break
            self._send(200, rows)
        elif path == "/overview":
            self._send(200, overview_cache)
        elif path == "/full":
            self._send(200, full_cache)
        elif path == "/positions":
            saved = load_positions()
            if saved:
                self._send(200, saved)
            else:
                self._send(404, {"error": "no saved layout"})
        elif path == "/node":
            qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            name = (qs.get("id") or [""])[0]
            if not name:
                self._send(400, {"error": "missing ?id=<localname>"})
            else:
                self._send(200, node_details(name))
        elif path in STATIC:
            name, ctype = STATIC[path]
            fp = HERE / name
            if not fp.exists():
                self._send(404, {"error": f"{name} not found — "
                                 "run compute_layout.py for full_positions.bin"})
            else:
                self._send(200, fp.read_bytes(), content_type=ctype)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            path = self.path.split("?", 1)[0]
            body = self._read_json()

            if path.startswith("/positions"):
                return self._send(200, save_positions(body))

            if path.startswith("/sparql"):
                # SPARQL panel sends {query}; return {vars, rows}.
                q = (body.get("query") or body.get("sparql") or "").strip()
                if not q:
                    return self._send(400, {"error": "missing 'query'"})
                return self._send(200, run_sparql(q))

            if path.startswith("/graph"):
                # CONSTRUCT -> force-graph JSON (per-Pod drilldown view).
                sparql = (body.get("sparql") or body.get("query") or "").strip()
                if not sparql:
                    return self._send(400, {"error": "missing 'sparql'"})
                if not sparql.lstrip().upper().startswith("PREFIX"):
                    sparql = PREFIXES + sparql
                result = graph.query(sparql)
                if result.type != "CONSTRUCT":
                    return self._send(400, {"error": "/graph needs a CONSTRUCT query"})
                return self._send(200, construct_to_forcegraph(result.graph))

            self._send(404, {"error": "not found"})
        except Exception as exc:  # surface query errors to the browser console
            self._send(400, {"error": f"{type(exc).__name__}: {exc}"})


def main():
    print(f"Loading {ABOX.name} into rdflib (one-time, ~30-60 s)...", flush=True)
    graph.parse(ABOX, format="turtle")
    print(f"Loaded {len(graph):,} triples.", flush=True)

    global overview_cache, full_cache
    print("Building class-level overview...", flush=True)
    overview_cache = build_overview()
    print(f"Overview ready: {len(overview_cache['nodes'])} classes, "
          f"{len(overview_cache['links'])} aggregated edges.", flush=True)

    print("Building full instance graph...", flush=True)
    full_cache = build_full()
    print(f"Full graph ready: {full_cache['count']:,} nodes, "
          f"{len(full_cache['links']) // 2:,} edges.", flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Serving on http://localhost:{PORT}  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
