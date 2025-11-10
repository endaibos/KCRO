import os
import logging
from pathlib import Path
import argparse
import re
import fnmatch
import yaml
import tempfile
import subprocess
import shutil
from github import Github
from github import UnknownObjectException, GithubException, ContentFile
from tqdm import tqdm
from dotenv import load_dotenv


load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OUTPUT_DIR = Path("./kubernetes_configs")
DEFAULT_MAX_CONTEXTS = 500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("scraper.log"),
        logging.StreamHandler()
    ]
)

ALLOWED_LICENSES = [
  "mit",
  "apache-2.0",
  "bsd-2-clause",
  "bsd-3-clause",
  "isc",
  "unlicense"
]

def is_license_allowed(repo) -> bool:
    """
    Check if the repository has a permissive license.
    """
    try:
        license_info = repo.get_license()
        if license_info and license_info.license:
            key = license_info.license.key.lower()
            if key in ALLOWED_LICENSES:
                return True
            else:
                logging.info(f"Skipping {repo.full_name}: license '{key}' not allowed.")
                return False
    except UnknownObjectException:
        logging.info(f"Skipping {repo.full_name}: no license detected.")
        return False
    except GithubException as e:
        logging.warning(f"Could not check license for {repo.full_name}: {e}")
        return False


def setup_github_client():
    """
    Initialize and authenticate GitHub client.
    """
    if not GITHUB_TOKEN:
        logging.error("GITHUB_TOKEN environment variable not set.")
        raise ValueError("Missing GitHub Personal Access Token")
    try:
        g = Github(GITHUB_TOKEN)
        user = g.get_user()
        logging.info(f"Authenticated as GitHub user: {user.login}")
        rate_limit = g.get_rate_limit()
        logging.info(f"Initial rate limit: {rate_limit.core.remaining}/{rate_limit.core.limit}")
        if rate_limit.core.remaining < 50 or rate_limit.search.remaining < 5:
            logging.warning("Rate limit is low. Consider waiting before running.")
        return g
    except GithubException as e:
        logging.error(f"Failed to authenticate or get user info: {e}")
        raise


def sanitize_path_part(part):
    """
    Sanitize a path component for safe local file paths.
    """
    part = part.replace("/", "_").replace("\\", "_")
    part = re.sub(r'[<>:"|?*\s]+', '_', part)
    part = part.strip('_.')
    return part


def save_manifest_file(content_file: ContentFile, target_dir: Path):
    """
    Save a GitHub ContentFile to the local filesystem.
    """
    try:
        original_filename = Path(content_file.path).name
        local_path = target_dir / original_filename
        target_dir.mkdir(parents=True, exist_ok=True)

        if content_file.content is None:
            logging.warning(f"Content is None for {content_file.repository.full_name}/{content_file.path}. Skipping.")
            return False

        content_bytes = content_file.decoded_content
        with open(local_path, "wb") as f:
            f.write(content_bytes)
        logging.info(f"Saved manifest: {local_path}")
        return True

    except Exception as e:
        logging.error(f"Error saving manifest {content_file.repository.full_name}/{content_file.path}: {e}", exc_info=True)
        return False


def get_repo_contents_recursive(repo, path: str):
    """
    Recursively retrieve all files starting from 'path' using Git tree.
    """
    try:
        try:
            branch = repo.get_branch("main").commit.sha
        except GithubException:
            branch = repo.get_branch("master").commit.sha

        tree = repo.get_git_tree(branch, recursive=True).tree

        for item in tree:
            if item.type == "blob" and item.path.startswith(path):
                yield repo.get_contents(item.path)
    except Exception as e:
        logging.warning(f"Failed to fetch tree for {repo.full_name}/{path}: {e}")


# --- Helm rendering support ---
def render_helm_chart_local(chart_dir: Path, output_dir: Path, values_file: Path = None):
    """
    Render a local Helm chart to YAML manifests.
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        rendered_file = output_dir / f"{chart_dir.name}_rendered.yaml"

        # Update chart dependencies
        subprocess.run(
            ["helm", "dependency", "build", str(chart_dir)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # Render the chart
        cmd = ["helm", "template", str(chart_dir)]
        if values_file and values_file.exists():
            cmd += ["--values", str(values_file)]

        result = subprocess.run(
            cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        # Save output to file
        with open(rendered_file, "wb") as f:
            f.write(result.stdout)

        logging.info(f"Helm chart rendered successfully: {rendered_file}")
        return True

    except FileNotFoundError:
        logging.error("Helm binary not found. Please install Helm (https://helm.sh/docs/intro/install/)")
        return False
    except subprocess.CalledProcessError as e:
        logging.error(f"Helm rendering failed for {chart_dir}: {e.stderr.decode('utf-8', errors='ignore')}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error rendering Helm chart {chart_dir}: {e}", exc_info=True)
        return False


def download_chart_to_tempdir(repo, chart_dir_path: Path):
    """
    Recursively download a Helm chart to a temporary directory.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="helmchart_"))
    try:
        def fetch_dir(remote_path, local_path):
            contents = repo.get_contents(str(remote_path))
            for item in contents:
                dest = local_path / item.name
                if item.type == "file":
                    local_path.mkdir(parents=True, exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(item.decoded_content)
                elif item.type == "dir":
                    fetch_dir(item.path, dest)
        fetch_dir(chart_dir_path, temp_dir)
        # Verify Chart.yaml exists
        if not any((temp_dir / f).name == "Chart.yaml" for f in temp_dir.iterdir()):
            logging.error(f"Downloaded chart has no Chart.yaml in {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None
        return temp_dir
    except Exception as e:
        logging.error(f"Failed to download chart {chart_dir_path}: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None


def process_helm_chart(chart_file: ContentFile, output_base_dir: Path, render: bool = True):
    """
    Process a Helm chart: either render it or download templates.
    """
    repo = chart_file.repository
    chart_dir = Path(chart_file.path).parent
    chart_name = chart_dir.name if chart_dir.name else "root_chart"
    logging.info(f"Processing Helm chart: {repo.full_name}/{chart_dir}")

    context_name = f"helm_{sanitize_path_part(repo.full_name)}_{sanitize_path_part(str(chart_dir))}"
    target_dir = output_base_dir / context_name
    target_dir.mkdir(parents=True, exist_ok=True)

    if render:
        local_chart_dir = download_chart_to_tempdir(repo, chart_dir)
        if not local_chart_dir:
            return 0
        success = render_helm_chart_local(local_chart_dir, target_dir)
        shutil.rmtree(local_chart_dir, ignore_errors=True)
        if success:
            logging.info(f"Rendered Helm chart for {repo.full_name}/{chart_dir}")
        return len(list(target_dir.rglob("*.yaml")))

    downloaded_count = 0
    for content_file in get_repo_contents_recursive(repo, str(chart_dir / "templates")):
        if content_file.name.lower().endswith((".yaml", ".yml")):
            if save_manifest_file(content_file, target_dir):
                downloaded_count += 1
    return downloaded_count


# --- Skaffold ---
def process_skaffold_config(skaffold_file: ContentFile, output_base_dir: Path):
    """
    Process a Skaffold configuration and download manifests.
    """
    repo = skaffold_file.repository
    skaffold_dir = Path(skaffold_file.path).parent
    context_name = f"skaffold_{sanitize_path_part(repo.full_name)}_{sanitize_path_part(str(skaffold_dir))}"
    target_dir = output_base_dir / context_name
    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded_count = 0

    try:
        skaffold_data = yaml.safe_load(skaffold_file.decoded_content.decode("utf-8"))
        deploy = skaffold_data.get("deploy", {})

        # --- kubectl.manifests ---
        if "kubectl" in deploy:
            manifests = deploy["kubectl"].get("manifests", [])
            downloaded_count += _download_kubectl_manifests(repo, skaffold_dir, manifests, target_dir)

        # --- helm.releases ---
        if "helm" in deploy:
            releases = deploy["helm"].get("releases", [])
            for release in releases:
                chart_path = release.get("chartPath")
                if not chart_path:
                    continue
                try:
                    contents = repo.get_contents(str(skaffold_dir / chart_path))
                    if isinstance(contents, list):
                        chart_yaml = next((f for f in contents if f.name == "Chart.yaml"), None)
                        if chart_yaml:
                            downloaded_count += process_helm_chart(chart_yaml, output_base_dir)
                    elif isinstance(contents, ContentFile) and contents.name == "Chart.yaml":
                        downloaded_count += process_helm_chart(contents, output_base_dir)
                except Exception as e:
                    logging.warning(f"Failed to process Helm release in {skaffold_file.path}: {e}")

        # --- kustomize.paths ---
        if "kustomize" in deploy:
            paths = deploy["kustomize"].get("paths", [])
            for path in paths:
                try:
                    abs_path = str(skaffold_dir / path)
                    contents = repo.get_contents(abs_path)
                    for f in contents if isinstance(contents, list) else [contents]:
                        if f.name.lower().endswith((".yaml", ".yml")):
                            if save_manifest_file(f, target_dir):
                                downloaded_count += 1
                except Exception as e:
                    logging.warning(f"Error processing Kustomize path {path}: {e}")

    except Exception as e:
        logging.error(f"Failed to process skaffold.yaml {skaffold_file.path}: {e}", exc_info=True)

    return downloaded_count


def _download_kubectl_manifests(repo, skaffold_dir: Path, manifest_paths_globs, target_dir: Path):
    """
    Download kubectl manifests from a Skaffold config.
    """
    downloaded_count = 0
    for path_glob in manifest_paths_globs:
        is_glob = "*" in path_glob or "?" in path_glob or "[" in path_glob
        manifest_path = skaffold_dir / path_glob
        try:
            if is_glob:
                dir_path = str(manifest_path.parent)
                contents = list(get_repo_contents_recursive(repo, dir_path))
                for f in contents:
                    if fnmatch.fnmatch(Path(f.path).name, manifest_path.name):
                        if save_manifest_file(f, target_dir):
                            downloaded_count += 1
            else:
                f = repo.get_contents(str(manifest_path))
                if f.name.lower().endswith((".yaml", ".yml")):
                    if save_manifest_file(f, target_dir):
                        downloaded_count += 1
        except Exception as e:
            logging.warning(f"Error downloading manifest {manifest_path}: {e}")
    return downloaded_count


# --- Main search ---
def search_and_process_contexts(g: Github, query: str, mode: str, output_dir: Path, max_contexts: int):
    """
    Search GitHub code and process Helm/Skaffold contexts.
    """
    logging.info(f"Starting search for {mode} with query: {query}")
    output_dir.mkdir(parents=True, exist_ok=True)
    processed = 0
    total_files = 0

    try:
        results = g.search_code(query)
        total = min(results.totalCount, 1000)
        bar = tqdm(results, total=total, desc=f"Processing {mode}", unit="context")

        for item in bar:
            if processed >= max_contexts:
                break
            repo = item.repository

            if not is_license_allowed(repo):
                continue  

            try:
                content = repo.get_contents(item.path)
                if mode == "helm":
                    total_files += process_helm_chart(content, output_dir, render=True)
                elif mode == "skaffold":
                    total_files += process_skaffold_config(content, output_dir)
                processed += 1
            except Exception as e:
                logging.warning(f"Error processing {repo.full_name}/{item.path}: {e}")
        bar.close()
        logging.info(f"Processed {processed} contexts, {total_files} YAMLs total.")
    except Exception as e:
        logging.error(f"Search failed: {e}", exc_info=True)


def remove_empty_dirs(path: Path):
    """
    Recursively delete empty directories.
    """
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            remove_empty_dirs(child)
    if path.is_dir() and not any(path.iterdir()):
        try:
            path.rmdir()
            logging.info(f"Removed empty directory: {path}")
        except Exception as e:
            logging.warning(f"Failed to remove empty directory {path}: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scrape Kubernetes manifests from GitHub Helm/Skaffold projects."
    )
    parser.add_argument("--mode", choices=["helm", "skaffold", "files"], default=None)
    parser.add_argument("-q", "--query", type=str)
    parser.add_argument("-o", "--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("-m", "--max-contexts", type=int, default=DEFAULT_MAX_CONTEXTS)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    BASE_QUERIES = {
        "helm": "filename:Chart.yaml",
        "skaffold": "filename:skaffold.yaml",
        "files": "language:YAML apiVersion kind"
    }

    SIZE_RANGES = []
    for start in range(0, 5000000, 100):
        SIZE_RANGES.append((start, start + 100))

    def generate_queries(base_query: str):
        queries = []
        for low, high in SIZE_RANGES:
            if high is None:
                queries.append(f"{base_query} size:>{low}")
            else:
                queries.append(f"{base_query} size:{low}..{high}")
        return queries

    try:
        github_client = setup_github_client()

        modes_to_process = [args.mode] if args.mode else ["helm", "skaffold", "files"]

        for mode in modes_to_process:
            base_query = args.query if args.query else BASE_QUERIES[mode]
            queries = generate_queries(base_query)

            logging.info(f"Processing mode: {mode}")

            for q in queries:
                logging.info(f"Executing query: {q}")
                search_and_process_contexts(
                    github_client,
                    q,
                    mode,
                    args.output_dir,
                    args.max_contexts
                )

        remove_empty_dirs(args.output_dir)
        logging.info("Pipeline complete.")

    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
