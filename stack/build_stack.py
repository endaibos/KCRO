#!/usr/bin/env python3
"""
Convert substratusai/the-stack-yaml-k8s into corpus JSONL for instantiate_kcro.py.

    python build_stack.py --out corpora/stack.jsonl

No reconstruction: the dataset ships the file text, so this only downloads (~310 MB
over five parquet shards, cached by HuggingFace) and reshapes columns.

Each output line:
  {"repo_full_name": "stack/<max_stars_repo_name>", "path": ..., "commit": ...,
   "corpus": "stack", "host": "github", "license": ..., "stars": ...,
   "content": "<raw YAML>"}
"""
import argparse, json
from pathlib import Path

from datasets import load_dataset

DATASET = "substratusai/the-stack-yaml-k8s"
CONTENT_COLS = ("content", "text", "file_content", "code")
PATH_COLS = ("max_stars_repo_path", "path", "file_path")
REPO_COLS = ("max_stars_repo_name", "repo_name", "repository")


def pick(cols, candidates, what):
    for c in candidates:
        if c in cols:
            return c
    raise SystemExit(f"no {what} column found; dataset has: {sorted(cols)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="corpora/stack.jsonl")
    ap.add_argument("--limit", type=int, help="stop after N rows (trial run)")
    a = ap.parse_args()

    ds = load_dataset(DATASET, split="train")
    cols = set(ds.column_names)
    print(ds)
    print("columns:", ds.column_names)

    c_content = pick(cols, CONTENT_COLS, "content")
    c_path = pick(cols, PATH_COLS, "path")
    c_repo = pick(cols, REPO_COLS, "repo")
    print(f"using content={c_content} path={c_path} repo={c_repo}")

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    with out.open("w") as fh:
        for row in ds:
            if a.limit and written >= a.limit:
                break
            content = row.get(c_content) or ""
            if not content.strip():
                skipped += 1
                continue
            fh.write(json.dumps({
                "repo_full_name": f"stack/{row.get(c_repo) or 'unknown'}",
                "path": row.get(c_path) or "?",
                "commit": row.get("max_stars_repo_head_hexsha"),
                "corpus": "stack",
                "host": "github",
                "license": row.get("max_stars_repo_licenses"),
                "stars": row.get("max_stars_count"),
                "content": content,
            }) + "\n")
            written += 1
    print(f"written {written}  empty skipped {skipped}  -> {out}")


if __name__ == "__main__":
    main()
