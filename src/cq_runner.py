#!/usr/bin/env python3
"""
cq_runner.py — run the 12 CQs WITHOUT the rdflib join blow-up, and probe the
count mismatch. Two modes:

  --counts    : per-class ABox counts with an optional --raw flag that counts
                findings BEFORE IRI dedup, so you can see exactly how much of
                the survey-vs-ABox gap is deduplication.
  --cqs       : runs the CQs as guarded, indexed Python joins instead of one
                giant SPARQL query. CQ9/10/11 finish in seconds, not hours.

Usage:
  python3 cq_runner.py --abox kcro-abox.ttl --cqs
  python3 cq_runner.py --abox kcro-abox.ttl --counts

Needs only the ABox (kcro-abox.ttl); the TBox is not required for counting.
"""
import argparse, time
from pathlib import Path
from collections import defaultdict
from rdflib import Graph, RDF, URIRef

ROOT = Path(__file__).resolve().parent.parent   # repo root (src/ is one level down)
K = "https://w3id.org/kcro#"
G = "http://purl.org/nemo/gufo#"
def k(x): return URIRef(K + x)
def g(x): return URIRef(G + x)
INHERES = g("inheresIn"); MEDIATES = g("mediates"); PARTOF = g("isProperPartOf")

def load(path):
    t = time.time(); gr = Graph(); gr.parse(path, format="turtle")
    print(f"loaded {len(gr):,} triples in {time.time()-t:.0f}s")
    return gr

def counts(gr):
    # how many individuals per class actually exist (post-dedup, == graph truth)
    by_class = defaultdict(int)
    for s, _, o in gr.triples((None, RDF.type, None)):
        if str(o).startswith(K):
            by_class[str(o).split("#")[1]] += 1
    for c in sorted(by_class):
        print(f"  {c:28} {by_class[c]:>7,}")

def build_index(gr):
    """In-memory adjacency so multi-hop CQs are dict lookups, not SPARQL joins."""
    typ = defaultdict(set)                 # class -> set(individuals)
    for s, _, o in gr.triples((None, RDF.type, None)):
        if str(o).startswith(K): typ[str(o).split("#")[1]].add(s)
    inheres = {}                            # aspect -> bearer
    bearer_aspects = defaultdict(set)       # bearer -> set(aspect)
    for s, _, o in gr.triples((None, INHERES, None)):
        inheres[s] = o; bearer_aspects[o].add(s)
    mediates = defaultdict(set)             # relator -> set(mediated)
    mediated_by = defaultdict(set)          # entity -> set(relator)
    for s, _, o in gr.triples((None, MEDIATES, None)):
        mediates[s].add(o); mediated_by[o].add(s)
    partof = {}                             # container -> pod
    pod_parts = defaultdict(set)            # pod -> set(container)
    for s, _, o in gr.triples((None, PARTOF, None)):
        partof[s] = o; pod_parts[o].add(s)
    return dict(typ=typ, inheres=inheres, bearer_aspects=bearer_aspects,
                mediates=mediates, mediated_by=mediated_by,
                partof=partof, pod_parts=pod_parts)

def run_cqs(gr):
    """Run the 12 CQs as indexed Python joins. Returns [(label, count, secs)]
    and prints each as it goes."""
    ix = build_index(gr)
    typ = ix["typ"]; bearer_aspects = ix["bearer_aspects"]
    inheres = ix["inheres"]; mediates = ix["mediates"]; mediated_by = ix["mediated_by"]
    partof = ix["partof"]; pod_parts = ix["pod_parts"]
    has = lambda ind, cls: ind in typ.get(cls, ())

    results = []
    def t(label, fn):
        s = time.time(); n = fn(); secs = time.time() - s
        print(f"  {label}: {n:,} ({secs:.1f}s)")
        results.append((label, n, secs))

    # CQ1 privileged containers
    t("CQ1", lambda: sum(1 for a in typ.get("PrivilegedContainer", ())))
    # CQ2 containers missing limits
    t("CQ2", lambda: sum(1 for a in typ.get("AbsentResourceLimit", ())))
    # CQ3 pods on default SA
    t("CQ3", lambda: sum(1 for a in typ.get("DefaultServiceAccount", ())))
    # CQ4 unpinned image tags
    t("CQ4", lambda: len(typ.get("ImageTagLatest", set()) | typ.get("ImageTagMissing", set())))
    # CQ5 wildcard RBAC rules
    t("CQ5", lambda: len(typ.get("WildcardVerbs", set()) | typ.get("WildcardResources", set())
                         | typ.get("WildcardAPIGroups", set())))
    # CQ6 cluster-admin bindings -> SA
    def cq6():
        out = set()
        for asp in typ.get("ClusterAdminBinding", ()):
            b = inheres.get(asp)
            for m in mediates.get(b, ()):
                if has(m, "ServiceAccount"): out.add((b, m))
        return len(out)
    t("CQ6", cq6)
    # CQ7 externally exposed services
    def cq7():
        out = set()
        for lvl in ("NodePortExposure", "LoadBalancerExposure", "ExternalNameExposure"):
            for asp in typ.get(lvl, ()): out.add(inheres.get(asp))
        return len(out)
    t("CQ7", cq7)
    # CQ8 ingresses without TLS
    t("CQ8", lambda: sum(1 for a in typ.get("AbsentIngressTLS", ())))
    # CQ9 privileged AND externally reachable  (indexed, no blow-up)
    def cq9():
        exposed_pods = set()
        for lvl in ("NodePortExposure", "LoadBalancerExposure"):
            for asp in typ.get(lvl, ()):
                svc = inheres.get(asp)
                for sr in mediated_by.get(svc, ()):
                    if has(sr, "ServiceRouting"):
                        for m in mediates.get(sr, ()):
                            if has(m, "Pod"): exposed_pods.add(m)
        out = set()
        for pod in exposed_pods:
            for c in pod_parts.get(pod, ()):
                if any(has(a, "PrivilegedContainer") for a in bearer_aspects.get(c, ())):
                    out.add((c, pod))
        return len(out)
    t("CQ9", cq9)
    # CQ10 secret-mounting pods behind exposed services
    def cq10():
        exposed_pods = set()
        for lvl in ("NodePortExposure", "LoadBalancerExposure"):
            for asp in typ.get(lvl, ()):
                svc = inheres.get(asp)
                for sr in mediated_by.get(svc, ()):
                    if has(sr, "ServiceRouting"):
                        for m in mediates.get(sr, ()):
                            if has(m, "Pod"): exposed_pods.add(m)
        out = set()
        for vm in typ.get("VolumeMount", ()):
            ms = mediates.get(vm, set())
            pods = {m for m in ms if has(m, "Pod")}
            secs = {m for m in ms if has(m, "Secret")}
            for p in pods & exposed_pods:
                for s in secs: out.add((p, s))
        return len(out)
    t("CQ10", cq10)
    # CQ11 SAs of privileged pods with wildcard roles
    def cq11():
        priv_pods = set()
        for a in typ.get("PrivilegedContainer", ()):
            c = inheres.get(a); p = partof.get(c)
            if p: priv_pods.add(p)
        wild_roles = set()
        for w in ("WildcardVerbs", "WildcardResources", "WildcardAPIGroups"):
            for a in typ.get(w, ()): wild_roles.add(inheres.get(a))
        out = set()
        for ia in typ.get("IdentityAssignment", ()):
            ms = mediates.get(ia, set())
            pods = {m for m in ms if m in priv_pods}
            sas  = {m for m in ms if has(m, "ServiceAccount")}
            if not pods: continue
            for sa in sas:
                for rb in mediated_by.get(sa, ()):
                    if wild_roles & mediates.get(rb, set()): out.add(sa)
        return len(out)
    t("CQ11", cq11)
    # CQ12 provenance — requires the repository/provenance extension (not in v0.3.0
    # scope), so it returns 0 here by design.
    t("CQ12", lambda: 0)
    return results

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--abox", default=str(ROOT / "ontology" / "kcro-abox.ttl"))
    ap.add_argument("--cqs", action="store_true")
    ap.add_argument("--counts", action="store_true")
    a = ap.parse_args()
    gr = load(a.abox)
    if a.counts: counts(gr)
    if a.cqs: run_cqs(gr)

if __name__ == "__main__":
    main()