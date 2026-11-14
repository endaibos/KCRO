#!/usr/bin/env python3
"""
Build the GitOps repository corpus: the one corpus in this work collected rather
than reconstructed, so every step records its own provenance.

    python collect_gitops.py discover --target 100     # query GitHub  -> CANDIDATES.json
    python collect_gitops.py clone                     # fetch at a pinned SHA -> .cache/gitops/
    python collect_gitops.py build                     # walk YAML     -> corpora/gitops.jsonl

PROTOCOL.json accumulates the audit trail across all three stages: the verbatim
search queries, the UTC date each ran, the API endpoint, every filter and its
threshold, and per repository the commit SHA actually walked.

The fetch stage prefers `git clone --depth 1`. Where git is unusable (e.g. macOS
ships only the Xcode shim and its licence has not been accepted) it falls back to
the GitHub tarball API at an explicitly resolved commit SHA -- the same tree a
depth-1 clone would yield, with the SHA pinned rather than implied by HEAD. The
method actually used is recorded per run in PROTOCOL.json. REPOS.csv is the
reviewer-facing repo list.

ADMISSION RULE (a repo enters the corpus only if all hold):
  * not a fork, not archived                        -- excluded at query time
  * repository size <= --max-size-mb                -- keeps clones bounded
  * >= --min-manifests Kubernetes manifests found   -- checked after cloning
  * AND either carries a GitOps topic, or contains a Flux HelmRelease /
    Kustomization or an Argo CD Application manifest  -- evidence of real GitOps use

Auth comes from the `gh` CLI (`gh auth status`), so no token is stored here.
"""
import argparse, csv, io, json, os, re, subprocess, sys, tarfile, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PROTOCOL = HERE / "PROTOCOL.json"
CANDIDATES = HERE / "CANDIDATES.json"
REPOS_CSV = HERE / "REPOS.csv"
CACHE = HERE / ".cache" / "gitops"

# Discovery queries, recorded verbatim in PROTOCOL.json. fork/archived are excluded
# here so the exclusion is part of the query a reviewer can replay.
QUERIES = [
    "topic:gitops fork:false archived:false stars:>=10",
    "topic:fluxcd fork:false archived:false stars:>=5",
    "topic:flux-cd fork:false archived:false stars:>=5",
    "topic:argocd fork:false archived:false stars:>=5",
    "topic:argo-cd fork:false archived:false stars:>=5",
]
GITOPS_TOPICS = {"gitops", "fluxcd", "flux-cd", "argocd", "argo-cd", "flux", "argo"}

# GitOps controller CRDs: presence proves the repo is a deployment source, not a chart
FLUX_API = ("helm.toolkit.fluxcd.io", "kustomize.toolkit.fluxcd.io", "source.toolkit.fluxcd.io")
ARGO_API = ("argoproj.io",)
GITOPS_KINDS = {"HelmRelease", "Kustomization", "Application", "ApplicationSet", "GitRepository"}

SKIP_DIRS = {".git", "node_modules", "vendor", ".terraform", "testdata", "test-data"}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh(*args):
    """Call the gh CLI and parse JSON, so auth stays in gh's keyring."""
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"gh {' '.join(args)} failed:\n{r.stderr.strip()}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def git_usable():
    """Apple's git shim fails until the Xcode licence is accepted; detect that."""
    r = subprocess.run(["git", "--version"], capture_output=True, text=True)
    return r.returncode == 0 and "xcode" not in (r.stderr or "").lower()


def gh_token():
    r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("gh auth token failed; run `gh auth login`")
    return r.stdout.strip()


YAML_EXT = (".yaml", ".yml")
MAX_MEMBER_BYTES = 2 * 1024 * 1024      # a single manifest larger than this is not one


def fetch_tarball(full_name, sha, dest: Path, token):
    """Download repo@sha and extract just its YAML files under dest."""
    url = f"https://api.github.com/repos/{full_name}/tarball/{sha}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "kcro-gitops-collector/1.0",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        blob = r.read()
    n = 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for m in tf:
            if not m.isfile() or not m.name.endswith(YAML_EXT):
                continue
            if m.size > MAX_MEMBER_BYTES:
                continue
            rel = Path(*Path(m.name).parts[1:])          # strip the owner-repo-sha/ prefix
            if not rel.parts or any(part in SKIP_DIRS or part == ".." for part in rel.parts):
                continue
            target = (dest / rel).resolve()
            if not str(target).startswith(str(dest.resolve())):
                continue                                  # refuse path traversal
            target.parent.mkdir(parents=True, exist_ok=True)
            f = tf.extractfile(m)
            if f is None:
                continue
            target.write_bytes(f.read())
            n += 1
    return n


def load_protocol():
    if PROTOCOL.exists():
        return json.loads(PROTOCOL.read_text())
    return {"corpus": "gitops", "stages": {}}


def save_protocol(p):
    PROTOCOL.write_text(json.dumps(p, indent=2, sort_keys=False) + "\n")


# ------------------------------------------------------------------ discover
def discover(a):
    proto = load_protocol()
    seen, cands = {}, []
    per_query = {}
    for q in QUERIES:
        got = 0
        for page in range(1, 11):                      # search API caps at 1000 results
            data = gh("api", "-X", "GET", "search/repositories",
                      "-f", f"q={q}", "-f", "sort=stars", "-f", "order=desc",
                      "-f", "per_page=100", "-f", f"page={page}")
            items = data.get("items", [])
            if not items:
                break
            for it in items:
                got += 1
                full = it["full_name"]
                if full in seen:
                    continue
                if it.get("fork") or it.get("archived"):
                    continue                            # belt and braces: query said so too
                seen[full] = True
                cands.append({
                    "full_name": full,
                    "stars": it.get("stargazers_count"),
                    "size_kb": it.get("size"),
                    "topics": it.get("topics") or [],
                    "default_branch": it.get("default_branch"),
                    "pushed_at": it.get("pushed_at"),
                    "license": (it.get("license") or {}).get("spdx_id"),
                    "html_url": it.get("html_url"),
                    "found_by": q,
                })
            if len(items) < 100:
                break
            time.sleep(a.pause)
        per_query[q] = got
        print(f"{got:5} hits  {q}")

    cands.sort(key=lambda c: -(c["stars"] or 0))
    CANDIDATES.write_text(json.dumps(cands, indent=2) + "\n")
    proto["stages"]["discover"] = {
        "ran_at_utc": now(),
        "endpoint": "GET https://api.github.com/search/repositories (via gh api)",
        "queries": QUERIES,
        "hits_per_query": per_query,
        "sort": "stars desc",
        "candidates_unique": len(cands),
        "gh_version": subprocess.run(["gh", "--version"], capture_output=True, text=True)
                                .stdout.splitlines()[0],
        "note": "fork:false and archived:false are part of each query, not a later filter",
    }
    save_protocol(proto)
    print(f"\n{len(cands)} unique candidates -> {CANDIDATES.name}")


# --------------------------------------------------------------------- clone
def clone(a):
    proto = load_protocol()
    cands = json.loads(CANDIDATES.read_text())
    CACHE.mkdir(parents=True, exist_ok=True)
    use_git = git_usable() and not a.force_tarball
    token = None if use_git else gh_token()
    method = ("git clone --depth 1 --single-branch"
              if use_git else "GitHub tarball API at a resolved commit SHA")
    print(f"method: {method}\n")

    cloned, too_big, failed = [], [], []
    for c in cands:
        if len(cloned) >= a.target:
            break
        if (c["size_kb"] or 0) > a.max_size_mb * 1024:
            too_big.append(c["full_name"])
            continue
        dest = CACHE / c["full_name"].replace("/", "__")
        try:
            if use_git:
                if not dest.exists():
                    r = subprocess.run(
                        ["git", "clone", "--depth", "1", "--single-branch", "--quiet",
                         f"https://github.com/{c['full_name']}.git", str(dest)],
                        capture_output=True, text=True)
                    if r.returncode:
                        failed.append((c["full_name"], (r.stderr.strip().splitlines() or [""])[-1]))
                        continue
                sha = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                                     capture_output=True, text=True).stdout.strip()
                files = None
            else:
                # resolve the branch tip to an immutable SHA first, then pin the fetch to it
                head = gh("api", f"repos/{c['full_name']}/commits/{c['default_branch']}")
                sha = head["sha"]
                marker = dest / ".fetched_sha"
                if marker.exists() and marker.read_text().strip() == sha:
                    files = None                          # already have this exact tree
                else:
                    dest.mkdir(parents=True, exist_ok=True)
                    files = fetch_tarball(c["full_name"], sha, dest, token)
                    marker.write_text(sha + "\n")
        except Exception as e:                            # network, tar, or API trouble
            failed.append((c["full_name"], f"{type(e).__name__}: {e}"))
            continue

        c["commit"] = sha
        c["clone_path"] = str(dest.relative_to(HERE))
        cloned.append(c)
        extra = f"  {files} yaml" if files is not None else ""
        print(f"{len(cloned):3}/{a.target}  {c['full_name']:50} {sha[:10]}{extra}")
        time.sleep(a.pause)

    (HERE / "CLONED.json").write_text(json.dumps(cloned, indent=2) + "\n")
    proto["stages"]["clone"] = {
        "ran_at_utc": now(),
        "method": method,
        "method_note": ("tarball fallback used because git is unusable in this environment; "
                        "the SHA is resolved via GET /repos/{repo}/commits/{branch} and the "
                        "tarball is fetched at that SHA, so the tree is reproducible")
                       if not use_git else "HEAD of the default branch at fetch time",
        "yaml_only": not use_git,
        "target": a.target,
        "cloned": len(cloned),
        "skipped_too_big": {"threshold_mb": a.max_size_mb, "repos": too_big},
        "clone_failures": failed,
    }
    save_protocol(proto)
    print(f"\nfetched {len(cloned)}  too big {len(too_big)}  failed {len(failed)}")


# --------------------------------------------------------------------- build
def scan_repo(root: Path):
    """Return (manifests, gitops_hits, yaml_files) for one clone."""
    manifests, gitops_hits, yaml_files = [], [], 0
    for p in root.rglob("*"):
        if p.is_dir() or p.suffix not in (".yaml", ".yml"):
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yaml_files += 1
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            continue                                   # Helm templates, CI files, etc.
        typed = [d for d in docs
                 if isinstance(d, dict) and d.get("kind") and d.get("apiVersion")]
        if not typed:
            continue
        manifests.append((p, text))
        for d in typed:
            api = str(d.get("apiVersion", ""))
            if d.get("kind") in GITOPS_KINDS and (
                    any(api.startswith(x) for x in FLUX_API + ARGO_API)):
                gitops_hits.append(f"{d['kind']}@{api.split('/')[0]}")
    return manifests, gitops_hits, yaml_files


def build(a):
    proto = load_protocol()
    cloned = json.loads((HERE / "CLONED.json").read_text())
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    admitted, rejected, rows = [], [], 0
    with out.open("w") as fh:
        for c in cloned:
            root = HERE / c["clone_path"]
            if not root.exists():
                rejected.append({**{k: c[k] for k in ("full_name",)}, "reason": "clone missing"})
                continue
            manifests, hits, yaml_files = scan_repo(root)
            has_topic = bool(set(t.lower() for t in c["topics"]) & GITOPS_TOPICS)
            enough = len(manifests) >= a.min_manifests
            evidence = has_topic or bool(hits)
            if not (enough and evidence):
                rejected.append({
                    "full_name": c["full_name"], "manifests": len(manifests),
                    "gitops_crds": len(hits), "has_topic": has_topic,
                    "reason": "below min-manifests" if not enough else "no GitOps evidence",
                })
                continue

            for p, text in manifests:
                fh.write(json.dumps({
                    "repo_full_name": f"gitops/{c['full_name']}",
                    "path": str(p.relative_to(root)),
                    "commit": c["commit"],
                    "corpus": "gitops",
                    "host": "github",
                    "stars": c["stars"],
                    "license": c["license"],
                    "content": text,
                }) + "\n")
                rows += 1
            admitted.append({
                "full_name": c["full_name"], "commit": c["commit"],
                "stars": c["stars"], "license": c["license"],
                "topics": c["topics"], "found_by": c["found_by"],
                "pushed_at": c["pushed_at"], "yaml_files": yaml_files,
                "manifests": len(manifests), "gitops_crds": len(hits),
                "admitted_by": "topic" if has_topic else "crd",
            })
            print(f"{c['full_name']:55} {len(manifests):5} manifests  {len(hits):4} gitops CRDs")

    with REPOS_CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(admitted[0].keys()) if admitted else ["full_name"])
        w.writeheader()
        for r in admitted:
            w.writerow({**r, "topics": ";".join(r["topics"])})

    proto["stages"]["build"] = {
        "ran_at_utc": now(),
        "admission_rule": {
            "min_manifests": a.min_manifests,
            "manifest_definition": "YAML document with both apiVersion and kind",
            "gitops_evidence": f"a GitOps topic {sorted(GITOPS_TOPICS)} OR a manifest whose "
                               f"kind is in {sorted(GITOPS_KINDS)} under {list(FLUX_API+ARGO_API)}",
            "skipped_dirs": sorted(SKIP_DIRS),
            "unparseable_yaml": "skipped silently (Helm templates are not plain YAML)",
        },
        "repos_admitted": len(admitted),
        "repos_rejected": rejected,
        "manifest_rows_written": rows,
        "out": str(out.relative_to(HERE)) if out.is_absolute() else str(out),
    }
    save_protocol(proto)
    print(f"\nadmitted {len(admitted)} repos  rejected {len(rejected)}  rows {rows} -> {out}")
    print(f"repo list -> {REPOS_CSV.name}   audit trail -> {PROTOCOL.name}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover"); d.add_argument("--target", type=int, default=100)
    d.add_argument("--pause", type=float, default=1.0); d.set_defaults(fn=discover)
    c = sub.add_parser("clone"); c.add_argument("--target", type=int, default=100)
    c.add_argument("--max-size-mb", type=int, default=300)
    c.add_argument("--pause", type=float, default=0.2)
    c.add_argument("--force-tarball", action="store_true",
                   help="use the tarball API even when git works")
    c.set_defaults(fn=clone)
    b = sub.add_parser("build"); b.add_argument("--out", default="corpora/gitops.jsonl")
    b.add_argument("--min-manifests", type=int, default=10); b.set_defaults(fn=build)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
