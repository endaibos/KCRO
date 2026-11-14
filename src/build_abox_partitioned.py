#!/usr/bin/env python3
"""
build_abox_partitioned.py --- build a large ABox without holding one graph in memory.

    python src/build_abox_partitioned.py --jsonl stack/corpora/stack.jsonl \
        --out ontology/kcro-abox-stack-v050.nt

Reference resolution in instantiate_kcro.py is already scoped to (repo, namespace):
by_name, pods_by_label and every deferred lookup key on the repo. Building one
repository at a time and concatenating is therefore exact, not an approximation --
no edge can cross a repository boundary, so none can be lost by the split. This is
partitioning, not sampling: every row is built, and whole repositories are kept
together in a partition (several small repositories may share one for speed).

Output is N-Triples, appended per partition, so memory stays flat at the size of
the largest partition rather than the whole corpus. The ABox ontology header is
written once rather than once per partition.
"""
import argparse, json, collections, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml
from rdflib import RDF, OWL

from instantiate_kcro import KCROGraph, ABOX_IRI      # reuse the shipped builder


def index_by_repo(path):
    """repo -> [byte offsets], so each repo's rows can be re-read on demand."""
    offsets = collections.defaultdict(list)
    with open(path, "rb") as fh:
        while True:
            pos = fh.tell()
            line = fh.readline()
            if not line:
                break
            try:
                repo = json.loads(line).get("repo_full_name") or "repo"
            except json.JSONDecodeError:
                continue
            offsets[repo].append(pos)
    return offsets


def rows_at(fh, offsets):
    for off in offsets:
        fh.seek(off)
        yield json.loads(fh.readline())


def docs_of(row):
    """Mirror load_jsonl's guards exactly, so partitioned == unpartitioned."""
    repo = row.get("repo") or row.get("repo_full_name") or "repo"
    try:
        docs = list(yaml.safe_load_all(row.get("content") or ""))
    except yaml.YAMLError:
        return
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind"):
            md = doc.get("metadata") or {}
            if not isinstance(md, dict):
                continue
            ns, name = md.get("namespace"), md.get("name", "?")
            if not all(v is None or isinstance(v, (str, int, float, bool))
                       for v in (ns, name)):
                continue
            yield repo, doc["kind"], ns, name, doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch-rows", type=int, default=4000,
                    help="approximate rows per partition; repos are never split")
    ap.add_argument("--census-out")
    a = ap.parse_args()

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    by_repo = index_by_repo(a.jsonl)
    print(f"{sum(len(v) for v in by_repo.values()):,} rows in {len(by_repo):,} repositories",
          flush=True)

    # header once, not per partition
    hdr = KCROGraph()
    with out.open("wb") as fh:
        fh.write(hdr.g.serialize(format="nt", encoding="utf-8"))
    header_triples = set(hdr.g)

    census = collections.Counter()
    partitions = done_rows = done_repos = 0
    batch, batch_rows = [], 0
    src = open(a.jsonl)

    def flush(batch):
        nonlocal partitions
        if not batch:
            return
        kg = KCROGraph()
        rows = [r for repo in batch for r in rows_at(src, by_repo[repo])]
        parsed = [t for row in rows for t in docs_of(row)]
        for repo, kind, ns, name, doc in parsed:          # pass 1
            kg.ingest(repo, kind, ns, name, doc)
        for repo, kind, ns, name, doc in parsed:          # label index
            kg.index_pod_labels(repo, ns, kind, name, doc)
        kg.resolve()                                      # pass 2
        for t in header_triples:
            kg.g.remove(t)
        with out.open("ab") as fh:
            fh.write(kg.g.serialize(format="nt", encoding="utf-8"))
        census.update(kg.counts)
        partitions += 1

    for repo, offs in by_repo.items():
        batch.append(repo); batch_rows += len(offs)
        if batch_rows >= a.batch_rows:
            flush(batch)
            done_rows += batch_rows; done_repos += len(batch)
            print(f"  {done_repos:,}/{len(by_repo):,} repos  {done_rows:,} rows  "
                  f"{partitions:,} partitions", flush=True)
            batch, batch_rows = [], 0
    flush(batch)
    done_repos += len(batch); done_rows += batch_rows
    src.close()

    # Some individuals are global by design (a container image is the same
    # individual wherever it is used), so an identical triple can be emitted by
    # more than one partition. RDF is a set, so the file is still correct, but the
    # triple count must be the DISTINCT count, not the line count.
    import hashlib
    TYPE = b"<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
    KCRO_NS = b"<https://w3id.org/kcro#"
    seen, lines = set(), 0
    typed = set()                       # (subject, class) pairs, for the true census
    with open(out, "rb") as fh:
        for line in fh:
            lines += 1
            seen.add(hashlib.blake2b(line.strip(), digest_size=16).digest())
            parts = line.split(b" ", 2)
            if len(parts) == 3 and parts[1] == TYPE and parts[2].startswith(KCRO_NS):
                typed.add((parts[0], parts[2].split(b">")[0]))
    triples = len(seen)
    # Summing kg.counts across partitions over-counts any individual that is global
    # rather than repo-scoped -- a ContainerImage IRI hashes the image string, so the
    # same individual is minted (and counted) in every partition that uses that image.
    # The census recomputed from the finished file is the authoritative one.
    file_census = collections.Counter()
    for _s, cls in typed:
        file_census[cls.decode().split("#")[-1]] += 1
    drift = {c: (census[c], file_census[c]) for c in file_census
             if census.get(c, 0) != file_census[c]}
    print(f"\nwrote {out}: {triples:,} distinct triples from {partitions:,} partitions "
          f"({done_repos:,} repos, {done_rows:,} rows)")
    print(f"  {lines:,} lines written; {lines - triples:,} are cross-partition repeats "
          f"of globally-scoped individuals")
    print(f"class census: {sum(file_census.values()):,} individuals across "
          f"{len(file_census)} classes (recomputed from the file)")
    if drift:
        print("  partition sums over-count these globally-scoped classes "
              "(summed -> actual):")
        for c, (a_, b_) in sorted(drift.items()):
            print(f"    {c:22} {a_:,} -> {b_:,}")
    if a.census_out:
        Path(a.census_out).write_text(json.dumps(dict(sorted(file_census.items())), indent=2) + "\n")
        print(f"census -> {a.census_out}")


if __name__ == "__main__":
    main()
