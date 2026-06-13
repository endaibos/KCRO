#!/usr/bin/env python3
"""
srq3.py — one-command full-corpus build + SRQ3 metrics + count verification.

Produces exactly the numbers the thesis needs:
  * pipeline coverage: % of corpus objects mapped to a KCRO class
  * KG size: triples in the generated ABox
  * per-class instance counts diffed against security_analysis.json
  * (--cqs) execution of the 12 competency questions with timings
Writes a markdown report you can paste from.

Usage (pick one input; use the venv's python so deps resolve):
  .venv/bin/python srq3.py --arrow k8s_dataset --analysis security_analysis.json --cqs --tbox kcro-abox.ttl
  .venv/bin/python srq3.py --jsonl corpus.jsonl --analysis security_analysis.json
  .venv/bin/python srq3.py                       # bare: defaults to --arrow k8s_dataset

Expects instantiate_kcro.py (v0.3.0 script) in the same directory. --cqs parses a
TBox (--tbox, default kcro.ttl); if you have no TBox, pass --tbox kcro-abox.ttl
(the CQs match explicit rdf:type triples, so no OWL reasoning is needed).
NOTE: the full corpus takes RAM and time (rdflib, in-memory). Expect on the
order of 10-30 minutes and a few GB; the CQ pass (esp. CQ9-CQ11) adds more.
"""

import argparse, json, time
from pathlib import Path

import instantiate_kcro as ik          # the v0.3.0 pipeline
from rdflib import Graph

PREFIXES = """PREFIX kcro: <https://w3id.org/kcro#>
PREFIX gufo: <http://purl.org/nemo/gufo#>
PREFIX prov: <http://www.w3.org/ns/prov#>
"""

# The 12 thesis CQs (final KCRO vocabulary) — same queries as website/visualizer.
CQS = {
 "CQ1": "SELECT ?c WHERE { ?v a kcro:PrivilegedContainer ; gufo:inheresIn ?c . ?c a kcro:Container . }",
 "CQ2": "SELECT ?p ?c WHERE { ?v a kcro:AbsentResourceLimit ; gufo:inheresIn ?c . ?c gufo:isProperPartOf ?p . }",
 "CQ3": "SELECT ?p WHERE { ?v a kcro:DefaultServiceAccount ; gufo:inheresIn ?p . ?p a kcro:Pod . }",
 "CQ4": "SELECT ?c ?t WHERE { ?q gufo:inheresIn ?c ; a ?t . VALUES ?t { kcro:ImageTagLatest kcro:ImageTagMissing } }",
 "CQ5": "SELECT ?r ?k WHERE { ?v gufo:inheresIn ?r ; a ?k . VALUES ?k { kcro:WildcardVerbs kcro:WildcardResources kcro:WildcardAPIGroups } ?r a ?rt . VALUES ?rt { kcro:Role kcro:ClusterRole } }",
 "CQ6": "SELECT ?b ?sa WHERE { ?v a kcro:ClusterAdminBinding ; gufo:inheresIn ?b . ?b gufo:mediates ?sa . ?sa a kcro:ServiceAccount . }",
 "CQ7": "SELECT ?s ?l WHERE { ?q gufo:inheresIn ?s ; a ?l . VALUES ?l { kcro:NodePortExposure kcro:LoadBalancerExposure kcro:ExternalNameExposure } ?s a kcro:Service . }",
 "CQ8": "SELECT ?i WHERE { ?v a kcro:AbsentIngressTLS ; gufo:inheresIn ?i . ?i a kcro:Ingress . }",
 "CQ9": """SELECT ?c ?s WHERE { ?e gufo:inheresIn ?s ; a ?l . VALUES ?l { kcro:NodePortExposure kcro:LoadBalancerExposure }
   ?sr a kcro:ServiceRouting ; gufo:mediates ?s, ?p . ?p a kcro:Pod . ?c gufo:isProperPartOf ?p .
   ?pv a kcro:PrivilegedContainer ; gufo:inheresIn ?c . }""",
 "CQ10": """SELECT ?p ?sec WHERE { ?vm a kcro:VolumeMount ; gufo:mediates ?p, ?sec . ?sec a kcro:Secret .
   ?sr a kcro:ServiceRouting ; gufo:mediates ?s, ?p . ?e gufo:inheresIn ?s ; a ?l .
   VALUES ?l { kcro:NodePortExposure kcro:LoadBalancerExposure } }""",
 "CQ11": """SELECT DISTINCT ?sa WHERE { ?ia a kcro:IdentityAssignment ; gufo:mediates ?p, ?sa . ?sa a kcro:ServiceAccount .
   ?c gufo:isProperPartOf ?p . ?pv a kcro:PrivilegedContainer ; gufo:inheresIn ?c .
   ?rb gufo:mediates ?sa, ?r . ?r a ?rt . VALUES ?rt { kcro:Role kcro:ClusterRole }
   ?w gufo:inheresIn ?r ; a ?wk . VALUES ?wk { kcro:WildcardVerbs kcro:WildcardResources kcro:WildcardAPIGroups } }""",
 "CQ12": """SELECT ?repo ?st WHERE { ?pv a kcro:PrivilegedContainer ; gufo:inheresIn ?c .
   ?c prov:wasDerivedFrom ?repo . ?repo a kcro:Repository ; kcro:repoStars ?st . }""",
}

# KCRO class -> candidate key names in security_analysis.json.
# *** Adjust the right-hand candidates to your json's actual key names. ***
ANALYSIS_KEYS = {
 "PrivilegedContainer":        ["container.securityContext.privileged=true", "privileged"],
 "PrivilegeEscalationAllowed": ["container.securityContext.allowPrivilegeEscalation=true", "allowPrivilegeEscalation"],
 "ContainerRunAsRoot":         ["container.securityContext.runAsUser=0", "container_runAsUser0"],
 "PodRunAsRoot":               ["pod.securityContext.runAsUser=0", "pod_runAsUser0"],
 "AbsentRunAsNonRoot":         ["pod.securityContext.runAsNonRoot=missing", "runAsNonRoot_missing"],
 "RunAsNonRootDisabled":       ["pod.securityContext.runAsNonRoot=false", "runAsNonRoot_false"],
 "AddedLinuxCapabilities":     ["container.capabilities.add=set", "capabilities_add"],
 "AbsentResourceLimit":        ["container.resources.limits=missing", "limits_missing"],
 "HostNetworkPod":             ["pod.hostNetwork=true", "hostNetwork"],
 "HostPIDPod":                 ["pod.hostPID=true", "hostPID"],
 "HostIPCPod":                 ["pod.hostIPC=true", "hostIPC"],
 "AutomountTokenEnabled":      ["pod.automountServiceAccountToken=missing", "automount_missing"],
 "DefaultServiceAccount":      ["pod.serviceAccount=default", "default_sa"],
 "WildcardVerbs":              ["rbac.rule.verbs=wildcard", "wildcard_verbs"],
 "WildcardResources":          ["rbac.rule.resources=wildcard", "wildcard_resources"],
 "WildcardAPIGroups":          ["rbac.rule.apiGroups=wildcard", "wildcard_apigroups"],
 "ClusterAdminBinding":        ["binding.roleRef.name=cluster-admin", "cluster_admin"],
 "AbsentIngressTLS":           ["ingress.tls=missing", "tls_missing"],
 "ImageTagLatest":             ["container.image.tag=latest", "tag_latest"],
 "ImageTagMissing":            ["container.image.tag=missing", "tag_missing"],
 "ClusterIPExposure":          ["service.type=ClusterIP"],
 "NodePortExposure":           ["service.type=NodePort"],
 "LoadBalancerExposure":       ["service.type=LoadBalancer"],
 "ExternalNameExposure":       ["service.type=ExternalName"],
}

def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict): out.update(flatten(v, key + "."))
        elif isinstance(v, (int, float)): out[key] = v
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl")
    ap.add_argument("--arrow", default="k8s_dataset",
                    help="HuggingFace Arrow dataset dir (default: k8s_dataset)")
    ap.add_argument("--analysis", help="security_analysis.json for count diffing")
    ap.add_argument("--tbox", default="kcro.ttl")
    ap.add_argument("--out", default="kcro-abox.ttl")
    ap.add_argument("--report", default="srq3_report.md")
    ap.add_argument("--cqs", action="store_true", help="also execute the 12 CQs (slow on full corpus)")
    a = ap.parse_args()

    # Validate the chosen corpus path up front, with a readable message.
    if not a.jsonl and not Path(a.arrow).exists():
        raise SystemExit(
            f"corpus not found: --arrow '{a.arrow}' does not exist.\n"
            f"Pass --arrow <dataset_dir> or --jsonl <file> "
            f"(the bundled corpus is the 'k8s_dataset' directory).")

    t0 = time.time()
    print("loading corpus…")
    rows = list(ik.load_jsonl(a.jsonl) if a.jsonl else ik.load_arrow(a.arrow))
    total = len(rows)
    mapped = sum(1 for _, kind, *_ in rows if kind in ik.KIND_CLASS)
    coverage = 100.0 * mapped / total if total else 0.0
    print(f"corpus objects: {total:,} | in-scope (mapped): {mapped:,} | coverage {coverage:.1f}%")

    print("pass 1 + 2…")
    kg = ik.KCROGraph()
    for repo, kind, ns, name, doc in rows: kg.ingest(repo, kind, ns, name, doc)
    for repo, kind, ns, name, doc in rows: kg.index_pod_labels(repo, ns, kind, name, doc)
    kg.resolve()
    triples = len(kg.g)
    print(f"ABox built: {triples:,} triples in {time.time()-t0:.0f}s — serialising…")
    kg.g.serialize(destination=a.out, format="turtle")
    under = kg.under_mediated()

    # ---- diff against the survey aggregates --------------------------------
    diff_lines = []
    if a.analysis:
        flat = flatten(json.loads(Path(a.analysis).read_text()))
        for cls, candidates in ANALYSIS_KEYS.items():
            got = kg.counts.get(cls, 0)
            exp = None
            for cand in candidates:
                hits = [v for k, v in flat.items() if k == cand or k.endswith("." + cand) or cand in k]
                if hits: exp = int(hits[0]); break
            mark = "?" if exp is None else ("OK" if exp == got else f"MISMATCH (survey {exp:,})")
            diff_lines.append(f"| {cls} | {got:,} | {exp if exp is not None else 'key not found'} | {mark} |")
            if exp is not None and exp != got:
                print(f"  !! {cls}: ABox {got:,} vs survey {exp:,}")

    # ---- CQ execution -------------------------------------------------------
    cq_lines = []
    if a.cqs:
        print("loading TBox + ABox for CQ pass…")
        g = Graph(); g.parse(a.tbox, format="turtle"); g.parse(a.out, format="turtle")
        for cid, q in CQS.items():
            t = time.time()
            try:
                nres = sum(1 for _ in g.query(PREFIXES + q))
                cq_lines.append(f"| {cid} | {nres:,} | {time.time()-t:.1f}s |")
                print(f"  {cid}: {nres:,} rows ({time.time()-t:.1f}s)")
            except Exception as e:
                cq_lines.append(f"| {cid} | error | {e} |")

    # ---- report --------------------------------------------------------------
    rep = [f"# SRQ3 full-corpus run\n",
           f"**Pipeline coverage:** {mapped:,} / {total:,} corpus objects mapped = **{coverage:.1f}%**\n",
           f"**KG size:** **{triples:,} triples** (`{a.out}`)\n",
           f"**Relators mediating < 2** (unresolved corpus references): {len(under):,}\n"]
    if diff_lines:
        rep += ["\n## Per-class counts vs security_analysis.json\n",
                "| class | ABox individuals | survey count | status |", "|---|---|---|---|", *diff_lines]
    if cq_lines:
        rep += ["\n## Competency-question execution\n", "| CQ | rows | time |", "|---|---|---|", *cq_lines]
    Path(a.report).write_text("\n".join(rep))
    print(f"\nreport written to {a.report}")
    print(f"THESIS NUMBERS -> coverage: {coverage:.1f}%  |  KG size: {triples:,} triples")

if __name__ == "__main__":
    main()