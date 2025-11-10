# KubeObjects: A Dataset of Real-World Kubernetes Objects

## Dataset Description

The Kubernetes Configs Dataset provides a large-scale, deduplicated collection of Kubernetes manifests mined from public GitHub repositories. Files include raw Kubernetes YAML, manifests from Helm charts (both raw and Helm-rendered), and Skaffold configurations. Each sample is enriched with repository metadata (stars, forks, license, language, timestamps) for downstream machine learning and security analysis tasks.

### Structure

- **Format:** HuggingFace `Dataset`
- **Deduplication Key:** (`author`, `repo_full_name`, `kind`, `metadata_name`, `metadata_namespace`)
- **Access:** Saved as HuggingFace/parquet files for direct loading into Python ML pipelines.

| Column                | Type   | Description |
|-----------------------|--------|-------------|
| content               | string | Raw YAML manifest |
| tool                  | string | Ingestion method (`helm`, `skaffold`, `files`) |
| author                | string | GitHub username |
| repo_full_name        | string | Repository full name |
| repo_sub_path         | string | Internal repo path to manifest |
| kind                  | string | K8s object kind (e.g., Deployment) |
| metadata_name         | string | Name of the object |
| metadata_namespace    | string | Namespace (default if absent) |
| container_images      | list   | Referenced container images |
| repo_hash             | string | SHA256 of {author}_{repo_full_name} |
| gh_stars              | float  | GitHub stars |
| gh_forks              | float  | GitHub forks |
| gh_language           | string | Repo language |
| gh_created_at         | string | Creation date (ISO) |
| gh_pushed_at          | string | Last push date (ISO) |
| gh_open_issues        | float  | Open GitHub issues |

## Collection & Processing Pipeline

1. **Ingestion:**  
   - Search & download manifests from GitHub using three profiles:
       - `filename:Chart.yaml` for Helm
       - `filename:skaffold.yaml` for Skaffold
       - `language:YAML apiVersion kind` for generic configs
   - Only permissive licenses (MIT, Apache, BSD, ISC, Unlicense).
   - Each config batch is labeled by acquisition tool.
   - For Helm, charts are either ingested as raw templates or locally rendered using the `helm template` CLI for fully populated YAML.

2. **Metadata Extraction:**  
   - Parse YAML files (multiple documents supported).
   - Extract K8s object metadata and referenced container images.
   - Enrich rows with repository statistics from the GitHub API.
   - Deduplicate manifests with: (`author`, `repo_full_name`, `kind`, `metadata_name`, `metadata_namespace`).

3. **Output:**  
   - Exported as HuggingFace Dataset directory (parquet).
   - Includes full YAML and all enrichment columns.

## Replicability: How to Reproduce

### Requirements

```bash
pip install -r requirements.txt
```

### Setup

1. Set GitHub token in `.env`:
    ```
    GITHUB_TOKEN=your_github_token
    ```

2. Download manifests:
    ```bash
    python scraper.py --mode helm --max-contexts 500
    python scraper.py --mode skaffold
    python scraper.py --mode files
    ```

3. Extract metadata and build dataset:
    ```bash
    python metadata.py
    ```

4. Load the dataset:
    ```python
    from datasets import load_dataset
    dataset = load_dataset('./k8s_hf_dataset')
    ```

## Example: Exploring the Dataset

```python
# List Kubernetes object kinds
kinds = set(dataset['kind'])

# Filter Deployments from popular repositories
deployments = dataset.filter(lambda x: x['kind'] == 'Deployment' and x['gh_stars'] > 100)
```