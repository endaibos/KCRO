#!/usr/bin/env python3
"""
consistency_by_parts.py --- HermiT over a large corpus, one group of repositories at a time.

    python src/consistency_by_parts.py --arrow data/k8s_dataset --corpus kubeobjects-v050

HermiT does not finish on the full KubeObjects ABox. Reference resolution in
instantiate_kcro.py never crosses a repository, so the ABox splits cleanly into
groups of whole repositories. Each group is built with the normal mapper and checked
against the TBox (with gUFO) by src/consistency_check.py.

What this shows: every part is consistent. It is not a proof for the union, because a
few individuals are shared between parts (a container image is one individual wherever
it is used). Those individuals carry no axioms that relate the parts to each other, but
the claim made is the per-part one.
"""
import argparse, collections, json, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import instantiate_kcro as ik


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl"); ap.add_argument("--arrow")
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--rows-per-part", type=int, default=1500,
                    help="approximate objects per part; repositories are never split")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per part")
    a = ap.parse_args()

    rows = list(ik.load_jsonl(a.jsonl) if a.jsonl else ik.load_arrow(a.arrow))
    by_repo = collections.defaultdict(list)
    for r in rows:
        by_repo[r[0]].append(r)
    meta = ik.load_repo_meta(a.arrow) if a.arrow else None

    parts, cur, n = [], [], 0
    for repo, rs in by_repo.items():
        cur.append(repo); n += len(rs)
        if n >= a.rows_per_part:
            parts.append(cur); cur, n = [], 0
    if cur:
        parts.append(cur)
    print(f"{len(rows):,} objects, {len(by_repo):,} repositories, {len(parts)} parts", flush=True)

    tmp = ROOT / "results" / f"part-{a.corpus}.nt"
    tmp_json = ROOT / "results" / f"part-{a.corpus}.json"
    records, t0 = [], time.time()
    for i, repos in enumerate(parts, 1):
        kg = ik.KCROGraph()
        prs = [r for repo in repos for r in by_repo[repo]]
        for repo, kind, ns, name, doc in prs:
            kg.ingest(repo, kind, ns, name, doc)
        for repo, kind, ns, name, doc in prs:
            kg.index_pod_labels(repo, ns, kind, name, doc)
        kg.resolve()
        if meta:
            kg.add_repo_meta(meta)
        kg.g.serialize(destination=str(tmp), format="nt")
        subprocess.run([sys.executable, str(ROOT / "src" / "consistency_check.py"),
                        "--abox", str(tmp), "--corpus", f"part-{a.corpus}",
                        "--out", str(tmp_json), "--timeout", str(a.timeout)],
                       capture_output=True, text=True)
        rec = json.loads(tmp_json.read_text())
        records.append({"part": i, "repositories": len(repos), "objects": len(prs),
                        "triples": len(kg.g), "verdict": rec["verdict"],
                        "reasoner_seconds": rec["reasoner_seconds"]})
        print(f"  part {i:3}/{len(parts)}  {len(repos):4} repos  {len(kg.g):7,} triples  "
              f"{rec['verdict']:12} {rec['reasoner_seconds']:6.1f}s", flush=True)
    tmp.unlink(missing_ok=True); tmp_json.unlink(missing_ok=True)

    verdicts = collections.Counter(r["verdict"] for r in records)
    out = ROOT / "results" / f"consistency-{a.corpus}-by-parts.json"
    out.write_text(json.dumps({
        "corpus": a.corpus, "parts": len(records), "verdicts": dict(verdicts),
        "objects": len(rows), "repositories": len(by_repo),
        "triples_sum_of_parts": sum(r["triples"] for r in records),
        "reasoner_seconds_total": round(sum(r["reasoner_seconds"] for r in records), 1),
        "wall_clock_seconds": round(time.time() - t0, 1),
        "note": "each part is a set of whole repositories; per-part consistency, "
                "not a proof for the union",
        "records": records}, indent=2) + "\n")
    print(f"\n{dict(verdicts)} over {len(records)} parts -> {out.name}")
    return 0 if set(verdicts) == {"consistent"} else 1


if __name__ == "__main__":
    sys.exit(main())
