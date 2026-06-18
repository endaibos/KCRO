import os
import yaml
from datasets import Dataset
from tqdm import tqdm
from github import Github
from dotenv import load_dotenv
import json
import hashlib
from github.GithubException import UnknownObjectException

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
ROOT_FOLDER = "./kubernetes_configs"
github_client = Github(GITHUB_TOKEN)
all_rows = []


def guess_repo_parts(folder_name, github_client):
    """
    Attempt to guess the tool, author, repo name, and internal path from the folder name.

    Example:
      helm_hmcts_hmcts-charts_stable_labs-endakelly-spring4
      -> tool='helm', author='hmcts', repo='hmcts-charts', subpath='stable/labs-endakelly-spring4'
    """
    parts = folder_name.split("_")
    if len(parts) < 3:
        return {"tool": None, "author": None, "repo_name": folder_name, "subpath": None, "repo_obj": None}

    tool = parts[0]
    rest = parts[1:]

    for i in range(1, len(rest)):
        author = "_".join(rest[:i])
        for j in range(i + 1, len(rest) + 1):
            repo_name = "_".join(rest[i:j])
            subpath = "_".join(rest[j:]) if j < len(rest) else None
            full_name = f"{author}/{repo_name}"

            try:
                repo_obj = github_client.get_repo(full_name)
                # Repo found, the rest is subpath
                return {
                    "tool": tool,
                    "author": author,
                    "repo_name": repo_name,
                    "subpath": subpath,
                    "repo_obj": repo_obj
                }
            except UnknownObjectException:
                continue
            except Exception:
                continue

    return {"tool": tool, "author": None, "repo_name": "_".join(rest), "subpath": None, "repo_obj": None}


def extract_images(doc):
    """
    Extract all container images from 'containers' and 'initContainers' in a Kubernetes manifest.
    """
    images = []

    def safe_get(d, path):
        for p in path:
            if isinstance(d, dict):
                d = d.get(p, {})
            else:
                return {}
        return d

    # Look in spec.template.spec.containers
    containers = safe_get(doc, ["spec", "template", "spec", "containers"])
    if isinstance(containers, list):
        for c in containers:
            if isinstance(c, dict) and "image" in c:
                images.append(c["image"])

    # Look in spec.template.spec.initContainers
    init_containers = safe_get(doc, ["spec", "template", "spec", "initContainers"])
    if isinstance(init_containers, list):
        for c in init_containers:
            if isinstance(c, dict) and "image" in c:
                images.append(c["image"])

    return images


missing_repos = []

# Iterate over repo folders
for repo_folder in tqdm(os.listdir(ROOT_FOLDER)[:10]):
    repo_path = os.path.join(ROOT_FOLDER, repo_folder)
    if not os.path.isdir(repo_path):
        continue

    tool, folderpath = repo_folder.split("_", 1)
    meta = guess_repo_parts(repo_folder, github_client)

    try:
        repo_obj = github_client.get_repo(f"{meta['author']}/{meta['repo_name']}")
    except Exception as e:
        repo_fullname = f"{meta['author']}/{meta['repo_name']}"
        print(f"GitHub repo not found for {repo_fullname}: {e}")
        missing_repos.append(repo_fullname)
        repo_obj = None

    gh_meta = {}
    if repo_obj:
        gh_meta = {
            "gh_stars": repo_obj.stargazers_count,
            "gh_forks": repo_obj.forks_count,
            "gh_language": repo_obj.language,
            "gh_created_at": repo_obj.created_at.isoformat(),
            "gh_pushed_at": repo_obj.pushed_at.isoformat() if repo_obj.pushed_at else None,
            "gh_open_issues": repo_obj.open_issues_count,
        }

    # Walk through all YAML files
    for root, _, files in os.walk(repo_path):
        for f in files:
            if f.lower().endswith((".yaml", ".yml")):
                file_path = os.path.join(root, f)
                try:
                    with open(file_path, "r", encoding="utf-8") as stream:
                        docs = list(yaml.safe_load_all(stream))
                except Exception as e:
                    print(f"Error reading file {file_path}: {e}")
                    continue

                for doc in docs:
                    if not isinstance(doc, dict):
                        continue

                    row = {
                        "content": yaml.dump(doc),
                        "tool": meta["tool"],
                        "author": meta["author"],
                        "repo_full_name": meta["repo_name"],
                        "repo_sub_path": meta["subpath"],
                        "kind": doc.get("kind"),
                        "metadata_name": doc.get("metadata", {}).get("name"),
                        "metadata_namespace": doc.get("metadata", {}).get("namespace", "default"),
                        "container_images": extract_images(doc),
                        "repo_hash": hashlib.sha256(f"{meta['author']}|{meta['repo_name']}|{meta['subpath']}".encode()).hexdigest(),
                        **gh_meta,
                    }
                    all_rows.append(row)

                    # Append row to JSON file
                    with open("all_rows.json", "a") as file:
                        json.dump(row, file, indent=2)

print(f"Found {len(all_rows)} Kubernetes objects.")

# Create HuggingFace-style dataset
if all_rows:
    dataset = Dataset.from_list(all_rows)
    print(f"Original rows: {len(dataset)}")

    # Columns used to define uniqueness
    subset_cols = ["author", "repo_full_name", "kind", "metadata_name", "metadata_namespace"]

    # Deduplicate
    seen = set()
    unique_list = []
    for row in dataset:
        key = tuple(row[c] for c in subset_cols)
        if key not in seen:
            seen.add(key)
            unique_list.append(row)

    # Create clean dataset
    dataset_clean = Dataset.from_list(unique_list)
    print(f"Rows after deduplication: {len(dataset_clean)}")

    dataset_clean.save_to_disk("k8s_hf_dataset")
    print("Dataset saved to 'k8s_hf_dataset'")
