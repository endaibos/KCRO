#!/usr/bin/env python3
"""
Reconstruct the Rahman et al. (TOSEM 2023) Kubernetes manifest corpus from the
URL tables in the figshare replication package, and emit JSONL in the shape
KCRO's instantiate_kcro.py --jsonl loader expects.

  python reconstruct_rahman.py GITHUB-URLS.csv GITLAB-URLS.csv \
      --out corpora/rahman.jsonl --cache .cache/rahman --deploy DEPLOY-REPOS.csv

Resumable: fetched files are cached on disk, so a re-run costs no requests.
Each output line:
  {"repo_full_name": "rahman/github/owner/repo", "path": ..., "commit": ...,
   "corpus": "rahman", "host": "github", "deploy": true|null, "content": "<raw YAML>"}
"""
import argparse, csv, hashlib, json, re, sys, time
from pathlib import Path
from http.client import HTTPException
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote

UA = {"User-Agent": "kcro-corpus-reconstruction/1.0"}


def enc(s: str) -> str:
    """Percent-encode a path/repo segment; '%' is safe so existing escapes survive."""
    return quote(s, safe="/%:@!$&'()*+,;=~-._")


def to_raw(url: str):
    """Return (host, repo, commit, path, raw_url) for a GitHub or GitLab blob URL."""
    url = url.strip()
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/blob/([0-9a-f]+)/(.+)$", url)
    if m:
        repo, commit, path = m.groups()
        return "github", repo, commit, path, f"https://raw.githubusercontent.com/{enc(repo)}/{commit}/{enc(path)}"
    # GitLab: group may be nested; blob segment is /blob/ or /-/blob/
    m = re.match(r"https?://gitlab\.com/(.+?)/(?:-/)?blob/([0-9a-f]+)/(.+)$", url)
    if m:
        repo, commit, path = m.groups()
        return "gitlab", repo, commit, path, f"https://gitlab.com/{enc(repo)}/-/raw/{commit}/{enc(path)}"
    return None


def fetch(raw_url: str, cache: Path, pause: float) -> str:
    key = hashlib.sha1(raw_url.encode()).hexdigest()
    blob = cache / key[:2] / key
    if blob.exists():
        return blob.read_text(errors="replace")
    time.sleep(pause)
    with urlopen(Request(raw_url, headers=UA), timeout=30) as r:
        text = r.read().decode("utf-8", errors="replace")
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_text(text)
    return text


def deploy_flags(path: Path | None):
    """repo short name -> True (deploys to k8s) / False, from DEPLOY-REPOS.csv."""
    flags = {}
    if not path:
        return flags
    with path.open() as fh:
        for row in csv.DictReader(fh):
            for col, val in row.items():
                if val and val.strip():
                    flags[val.strip()] = col.endswith("-DEPLOY")
    return flags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="+", help="GITHUB-URLS.csv and/or GITLAB-URLS.csv")
    ap.add_argument("--out", default="corpora/rahman.jsonl")
    ap.add_argument("--cache", default=".cache/rahman")
    ap.add_argument("--deploy", help="DEPLOY-REPOS.csv (optional)")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds between live fetches")
    ap.add_argument("--limit", type=int, help="stop after N rows (for a trial run)")
    args = ap.parse_args()

    cache = Path(args.cache)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    flags = deploy_flags(Path(args.deploy) if args.deploy else None)

    seen, written, failed, skipped = set(), 0, [], 0
    with out.open("w") as fh:
        for csv_path in args.urls:
            with open(csv_path, newline="") as src:
                for row in csv.DictReader(src):
                    if args.limit and written + len(failed) >= args.limit:
                        break
                    parsed = to_raw(row["YAML_URL"])
                    if not parsed:
                        failed.append((row["YAML_URL"], "unparseable URL"))
                        continue
                    host, repo, commit, path, raw_url = parsed
                    if (repo, commit, path) in seen:
                        skipped += 1
                        continue
                    seen.add((repo, commit, path))
                    try:
                        content = fetch(raw_url, cache, args.pause)
                    except (OSError, HTTPException, ValueError) as e:
                        failed.append((raw_url, str(e)))
                        continue
                    fh.write(json.dumps({
                        "repo_full_name": f"rahman/{host}/{repo}",
                        "path": path,
                        "commit": commit,
                        "corpus": "rahman",
                        "host": host,
                        "deploy": flags.get(repo.split("/")[-1]),
                        "content": content,
                    }) + "\n")
                    written += 1

    print(f"written {written}  duplicates skipped {skipped}  failed {len(failed)}")
    if failed:
        log = out.with_suffix(".failed.tsv")
        log.write_text("\n".join(f"{u}\t{e}" for u, e in failed))
        print(f"failures logged to {log}")


if __name__ == "__main__":
    main()
