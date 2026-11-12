#!/usr/bin/env python3
"""
Smoke-test a corpus JSONL before handing it to instantiate_kcro.py.

    python src/smoke_corpus.py rahman/corpora/rahman.jsonl
    python src/smoke_corpus.py stack/corpora/stack.jsonl --sample 20000

Checks, cheapest first:
  1. every line is JSON, has the keys the loader reads, and non-empty content
  2. the YAML actually parses and looks like Kubernetes (apiVersion/kind/metadata)
  3. the field names match what src/instantiate_kcro.py's --jsonl loader reads
  4. kind / host / deploy distributions, so you can eyeball corpus composition
Exit status is non-zero if any hard check fails.
"""
import argparse, json, sys, collections
from pathlib import Path

import yaml

REQUIRED = ("repo_full_name", "path", "commit", "corpus", "host", "content")
# what load_jsonl() in src/instantiate_kcro.py reads off each row
LOADER_REPO_KEY = "repo"

def main(path, sample=0):
    rows, bad_json, missing, empty = 0, [], collections.Counter(), 0
    kinds = collections.Counter()
    hosts = collections.Counter()
    deploy = collections.Counter()
    yaml_err, no_kind, multi_doc, k8s_docs = [], [], 0, 0
    dupes = collections.Counter()

    with open(path) as fh:
        for n, line in enumerate(fh, 1):
            if sample and n > sample:
                break
            line = line.strip()
            if not line:
                continue
            rows += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                bad_json.append((n, str(e)))
                continue

            for k in REQUIRED:
                if k == "content":          # emptiness is reported separately
                    if k not in row:
                        missing[k] += 1
                elif k not in row or row[k] in (None, ""):
                    missing[k] += 1
            if not (row.get("content") or "").strip():
                empty += 1                  # 0-byte placeholders exist upstream: a warning
                continue

            hosts[row.get("host")] += 1
            deploy[row.get("deploy")] += 1
            dupes[(row.get("repo_full_name"), row.get("commit"), row.get("path"))] += 1

            # layer 2: does the YAML parse, and is it Kubernetes?
            try:
                docs = [d for d in yaml.safe_load_all(row["content"])]
            except yaml.YAMLError as e:
                yaml_err.append((row.get("path"), str(e).split("\n")[0]))
                continue
            typed = [d for d in docs if isinstance(d, dict) and d.get("kind")]
            if len(docs) > 1:
                multi_doc += 1
            if not typed:
                no_kind.append(row.get("path"))
            k8s_docs += len(typed)
            for d in typed:
                kinds[d["kind"]] += 1

    hard_fail = bool(bad_json) or any(missing.values())

    print(f"rows                 {rows}")
    print(f"k8s docs (kind set)  {k8s_docs}   [one row can hold several]")
    print(f"multi-doc rows       {multi_doc}")
    print(f"distinct kinds       {len(kinds)}")
    print()
    print(f"malformed JSON lines {len(bad_json)}" + ("  <-- FAIL" if bad_json else ""))
    print(f"empty content        {empty}   [0-byte files upstream; loader yields nothing]"
          if empty else "empty content        0")
    for k, c in missing.items():
        print(f"missing/blank {k:16} {c}  <-- FAIL")
    print(f"YAML parse errors    {len(yaml_err)}   [Helm templates are expected here]")
    print(f"rows with no 'kind'  {len(no_kind)}")
    d = [k for k, c in dupes.items() if c > 1]
    print(f"duplicate (repo,commit,path) {len(d)}" + ("  <-- FAIL" if d else ""))

    # layer 3: field-name contract with the loader
    print()
    here = Path(__file__).resolve().parent
    src = next((c for c in (here / "instantiate_kcro.py",
                            here.parent / "src" / "instantiate_kcro.py") if c.exists()),
               here / "instantiate_kcro.py")
    if src.exists():
        text = src.read_text()
        if f'row.get("{LOADER_REPO_KEY}"' in text and "repo_full_name" not in text.split("def load_arrow")[0]:
            print(f'CONTRACT MISMATCH: load_jsonl() reads row["{LOADER_REPO_KEY}"], '
                  f'this file provides "repo_full_name".')
            print("  -> every row would fall back to the literal repo name \"repo\",")
            print("     collapsing all repos into one IRI namespace.")
            hard_fail = True
        else:
            print("loader field contract: ok")

    print()
    print("hosts   ", dict(hosts))
    print("deploy  ", {str(k): v for k, v in deploy.items()})
    print("top kinds")
    for k, c in kinds.most_common(15):
        print(f"  {k:28} {c}")
    if yaml_err[:5]:
        print("first YAML errors")
        for p, e in yaml_err[:5]:
            print(f"  {p}: {e[:80]}")

    return 1 if hard_fail else 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--sample", type=int, default=0, help="only read the first N lines")
    a = ap.parse_args()
    sys.exit(main(a.path, a.sample))
