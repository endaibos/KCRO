#!/usr/bin/env python3
"""
diagnose_dedup.py — settle whether the survey-vs-graph count gap is
DEDUPLICATION (defensible) or UNRESOLVED/DROPPED findings (a bug).

It re-scans the corpus exactly as the pipeline does for ONE finding class,
mints the same IRIs, and reports:
  * raw occurrences  (what survey.py counts)
  * distinct IRIs    (what the graph stores after dedup)
  * occurrences lost to dedup  (raw - distinct)  <- defensible
  * occurrences that produced NO individual       <- bug, must be 0

Run it on the cleanest case first (cluster-admin, gap of 5):
  python3 diagnose_dedup.py --jsonl corpus.jsonl --finding cluster-admin
  python3 diagnose_dedup.py --jsonl corpus.jsonl --finding privileged
"""
import argparse, json, hashlib, yaml
from collections import Counter

def h(*parts): return hashlib.sha1("/".join(str(p) for p in parts).encode()).hexdigest()[:12]

def iter_docs(path):
    with open(path) as f:
        for line in f:
            line=line.strip()
            if not line: continue
            row=json.loads(line)
            for doc in yaml.safe_load_all(row.get("content","")):
                if isinstance(doc,dict) and doc.get("kind"):
                    yield row.get("repo","repo"), doc

def scan_cluster_admin(path):
    raw=0; iris=Counter(); unresolved=[]
    for repo,doc in iter_docs(path):
        if doc.get("kind") not in ("RoleBinding","ClusterRoleBinding"): continue
        rr=(doc or {}).get("roleRef") or {}
        if rr.get("name")=="cluster-admin":
            raw+=1
            ns=(doc.get("metadata") or {}).get("namespace") or "default"
            name=(doc.get("metadata") or {}).get("name","?")
            # same key the pipeline uses for the ClusterAdminBinding aspect
            bearer=h(repo,doc["kind"],ns,name)
            iri=h(bearer,"ClusterAdminBinding","binding.roleRef.name=cluster-admin")
            iris[iri]+=1
    return raw, iris

def scan_privileged(path):
    raw=0; iris=Counter()
    WL={"Deployment","StatefulSet","DaemonSet","ReplicaSet","Job","CronJob"}
    for repo,doc in iter_docs(path):
        k=doc.get("kind")
        if k not in WL and k!="Pod": continue
        spec=(doc or {}).get("spec",{}) or {}
        podspec = spec if k=="Pod" else ((spec.get("template") or {}).get("spec") or {})
        ns=(doc.get("metadata") or {}).get("namespace") or "default"
        name=(doc.get("metadata") or {}).get("name","?")
        owner=h(repo,k,ns,name)
        pod = owner if k=="Pod" else h(owner,"Pod","template")
        for c in podspec.get("containers",[]) or []:
            csc=c.get("securityContext") or {}
            if isinstance(csc,dict) and csc.get("privileged") is True:
                raw+=1
                cont=h(pod,"Container",c.get("name","c"))
                iri=h(cont,"PrivilegedContainer","container.securityContext.privileged=true")
                iris[iri]+=1
    return raw, iris

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--jsonl",required=True)
    ap.add_argument("--finding",choices=["cluster-admin","privileged"],required=True)
    a=ap.parse_args()
    raw,iris = scan_cluster_admin(a.jsonl) if a.finding=="cluster-admin" else scan_privileged(a.jsonl)
    distinct=len(iris)
    lost_to_dedup=raw-distinct
    collisions={k:v for k,v in iris.items() if v>1}
    print(f"finding: {a.finding}")
    print(f"  raw occurrences (survey-style count): {raw}")
    print(f"  distinct individuals (graph stores):  {distinct}")
    print(f"  collapsed by dedup (raw - distinct):  {lost_to_dedup}")
    print(f"  IRIs that absorbed >1 occurrence:     {len(collisions)}")
    if collisions:
        ex=sorted(collisions.items(), key=lambda x:-x[1])[:5]
        print("  top collisions (IRI hash : how many occurrences merged):")
        for k,v in ex: print(f"    {k} : {v}")
    print()
    print("INTERPRETATION:")
    print(f"  If 'distinct' here == the graph's count for this class, the ENTIRE")
    print(f"  gap is deduplication -> defensible, write it as a design property.")
    print(f"  If 'distinct' here  > the graph's count, the difference is findings")
    print(f"  that were DROPPED (parse/resolve failure) -> investigate, it's a bug.")