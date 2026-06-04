"""
survey.py --- analysis tool for the KubeObjects dataset.

Modes
=====

The script supports three modes, controlled by command-line flags:

* **default** --- fast metadata overview. Prints kind counts, ingestion-tool
  distribution, language distribution, and basic size statistics. Reads only
  metadata columns; no YAML is parsed. Runs in seconds.

* **--security** --- security-relevant field analysis. Parses every manifest's
  ``content`` column as YAML, dispatches on ``kind``, and counts the fields
  identified in the SRQ1 outline as security-relevant: workload security
  context, network surface, RBAC wildcards, and configuration secrets.
  Also catalogues inter-resource relationship types. Slow (~2 min for 75k
  manifests). Writes results to ``security_analysis.json``.

* **--charts** --- generate PNG visualisations under ``figures/``. Implies
  ``--security`` (the security counters are needed to build the charts).
  Three PNGs are produced: top-20 kinds, security-field prevalence by
  category, and inter-resource relationship counts.

Usage::

    python survey.py                       # quick metadata overview
    python survey.py --security            # YAML-parsing deep dive
    python survey.py --security --charts   # plus PNG charts

Outputs
=======

* stdout --- human-readable tables and summaries.
* ``security_analysis.json`` --- machine-readable counters (when ``--security``).
* ``figures/*.png`` --- bar charts (when ``--charts``).
* ``parse_failures.log`` --- first 5 YAML parse errors (when ``--security``).

Field grounding
===============

The list of security-relevant fields is drawn from the SRQ1 thesis outline,
which itself is grounded in three sources: Rahman et al. 2024 (Kubernetes
misconfigurations), Aliforenko 2025 (graph-based K8s security analysis), and
Haque et al. 2022 (KGSecConfig). Each field counted here appears in at least
one of those references as security-relevant.

Defensive parsing
=================

Real-world manifests sometimes contain non-dict values where dicts are
expected --- typically unrendered Helm template strings (e.g. a
``securityContext:`` whose value is the literal string
``"{{ .Values.podSecurityContext }}"``) or malformed YAML. The ``as_dict``
and ``as_list`` helpers coerce such values to ``{}`` / ``[]`` so that
downstream ``.get()`` and iteration calls do not raise. Such fields are
treated as missing, since they cannot be inspected.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

# Optional imports gated on flags. matplotlib is heavy enough that we don't
# want to pay its import cost when the user just wants the metadata overview.
# It is imported lazily inside main() when --charts is set.

from datasets import load_from_disk

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATASET_DIR    = Path(__file__).parent / "k8s_dataset"
ARROW_FILE     = DATASET_DIR / "data-00000-of-00001.arrow"
OUTPUT_JSON    = Path(__file__).parent / "security_analysis.json"
FIGURES_DIR    = Path(__file__).parent / "figures"
PARSE_LOG      = Path(__file__).parent / "parse_failures.log"

# ---------------------------------------------------------------------------
# Workload kinds whose pod template needs security-context inspection.
# ---------------------------------------------------------------------------

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Pod", "Job", "CronJob", "ReplicaSet"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fmt(n: int) -> str:
    """Format an integer with thousands separators (e.g. ``75390 -> '75,390'``)."""
    return f"{n:,}"


def as_dict(v: Any) -> dict:
    """Return ``v`` if it is a dict, otherwise an empty dict.

    Some manifests in the corpus contain non-dict values where dicts are
    expected --- typically unrendered Helm template strings or malformed
    YAML. This helper guarantees that downstream ``.get()`` calls are safe
    regardless of what the YAML parser returned.
    """
    return v if isinstance(v, dict) else {}


def as_list(v: Any) -> list:
    """Return ``v`` if it is a list, otherwise an empty list.

    Same rationale as :func:`as_dict`, applied to keys whose value is
    expected to be a list (e.g. ``containers``, ``rules``, ``volumes``).
    """
    return v if isinstance(v, list) else []


def print_top(counter: Counter, label: str, n: int = 20) -> None:
    """Print the top-``n`` entries of a Counter as a labelled ASCII bar chart.

    Bars scale so the largest entry occupies 40 characters. Smaller entries
    scale proportionally; very small entries may render as no bar at all.
    """
    print(f"\n--- Top {n} {label} ---")
    top_count = counter.most_common(1)[0][1] if counter else 1
    for value, count in counter.most_common(n):
        bar = "#" * min(40, count * 40 // top_count)
        print(f"  {str(value):<40} {fmt(count):>8}  {bar}")


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict, returning ``default`` on any missing key or non-dict.

    Many K8s manifest fields are deeply nested
    (e.g. ``spec.template.spec.securityContext.privileged``). Using this helper
    keeps the analysis code linear without raising on every missing key, which
    is the common case --- most manifests do not set most security fields.
    """
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


# ---------------------------------------------------------------------------
# Mode 1: fast metadata overview (the original survey.py behaviour)
# ---------------------------------------------------------------------------


def run_overview(ds) -> None:
    """Print the metadata-only overview: kinds, tools, languages, sizes."""
    n_rows = len(ds)
    print(f"Total rows   : {fmt(n_rows)}")
    print(f"Columns      : {ds.column_names}")

    if n_rows == 0:
        print("\nDataset is EMPTY --- run scraper.py then metadata.py to populate it.")
        return

    kinds = Counter(ds["kind"])
    tools = Counter(ds["tool"])
    langs = Counter(v or "(unknown)" for v in ds["gh_language"])
    repos = len(set(ds["repo_full_name"]))

    content_sizes = [len(c) for c in ds["content"] if c]
    avg_kb   = sum(content_sizes) / len(content_sizes) / 1024 if content_sizes else 0
    total_mb = sum(content_sizes) / 1024 / 1024

    print(f"Unique kinds : {fmt(len(kinds))}")
    print(f"Unique repos : {fmt(repos)}")
    print(f"Content size : avg {avg_kb:.1f} KB / total {total_mb:.0f} MB")

    print_top(kinds, "Kubernetes kinds")
    print_top(tools, "Ingestion tools", n=len(tools))
    print_top(langs, "Repo languages", n=10)


# ---------------------------------------------------------------------------
# Mode 2: security analysis --- YAML parsing + field counting
# ---------------------------------------------------------------------------


def get_pod_spec(doc: dict) -> dict:
    """Locate the pod spec inside a workload manifest, regardless of kind.

    Returns ``{}`` if the spec is absent or malformed. Centralising this
    lookup prevents drift between :func:`analyse_workload` and
    :func:`analyse_relationships`, which both need it.
    """
    kind = doc.get("kind")
    if kind == "Pod":
        return as_dict(safe_get(doc, "spec"))
    if kind == "CronJob":
        return as_dict(safe_get(doc, "spec", "jobTemplate", "spec", "template", "spec"))
    # Deployment, StatefulSet, DaemonSet, Job, ReplicaSet
    return as_dict(safe_get(doc, "spec", "template", "spec"))


def analyse_workload(doc: dict, fields: Counter) -> None:
    """Count security-relevant fields in a workload manifest's pod template."""
    pod_spec = get_pod_spec(doc)
    if not pod_spec:
        return

    # Pod-level securityContext (applies to all containers in the pod).
    pod_sc = as_dict(pod_spec.get("securityContext"))
    if pod_sc.get("runAsUser") == 0:
        fields["pod.securityContext.runAsUser=0"] += 1
    if pod_sc.get("runAsNonRoot") is False:
        fields["pod.securityContext.runAsNonRoot=false"] += 1
    if pod_sc.get("runAsNonRoot") is None and pod_sc.get("runAsUser") != 0:
        fields["pod.securityContext.runAsNonRoot=missing"] += 1

    # Host-namespace flags --- pod-level, not container-level.
    if pod_spec.get("hostNetwork") is True:
        fields["pod.hostNetwork=true"] += 1
    if pod_spec.get("hostPID") is True:
        fields["pod.hostPID=true"] += 1
    if pod_spec.get("hostIPC") is True:
        fields["pod.hostIPC=true"] += 1

    # ServiceAccount usage. The default ServiceAccount is a known anti-pattern.
    sa = pod_spec.get("serviceAccountName") or pod_spec.get("serviceAccount")
    if sa is None or sa == "default":
        fields["pod.serviceAccount=default"] += 1

    if pod_spec.get("automountServiceAccountToken") is None:
        fields["pod.automountServiceAccountToken=missing"] += 1

    # Container-level checks. Both initContainers and containers count.
    containers = as_list(pod_spec.get("containers")) + as_list(pod_spec.get("initContainers"))
    for c in containers:
        if not isinstance(c, dict):
            continue

        c_sc = as_dict(c.get("securityContext"))
        if c_sc.get("privileged") is True:
            fields["container.securityContext.privileged=true"] += 1
        if c_sc.get("allowPrivilegeEscalation") is True:
            fields["container.securityContext.allowPrivilegeEscalation=true"] += 1
        if c_sc.get("runAsUser") == 0:
            fields["container.securityContext.runAsUser=0"] += 1

        caps = as_dict(c_sc.get("capabilities"))
        if as_list(caps.get("add")):
            fields["container.capabilities.add=set"] += 1

        # Image-tag hygiene. ``:latest`` and missing tags are both flagged.
        image = c.get("image") or ""
        if isinstance(image, str):
            if image.endswith(":latest") or ":latest@" in image:
                fields["container.image.tag=latest"] += 1
            elif ":" not in image.split("/")[-1]:
                fields["container.image.tag=missing"] += 1

        # Resource limits. Missing limits is a denial-of-service surface.
        if not as_dict(c.get("resources")).get("limits"):
            fields["container.resources.limits=missing"] += 1


def analyse_service(doc: dict, fields: Counter) -> None:
    """Count Service ``type`` distribution and external-exposure patterns."""
    svc_type = safe_get(doc, "spec", "type") or "ClusterIP"  # ClusterIP is the default
    fields[f"service.type={svc_type}"] += 1


def analyse_ingress(doc: dict, fields: Counter) -> None:
    """Count Ingresses without TLS configuration."""
    tls = safe_get(doc, "spec", "tls")
    if not tls:
        fields["ingress.tls=missing"] += 1


def analyse_rbac(doc: dict, fields: Counter) -> None:
    """Count RBAC rules with wildcard verbs/resources/apiGroups.

    Applies to Role and ClusterRole. Wildcard rules are the single most
    commonly cited RBAC misconfiguration in the literature.
    """
    rules = as_list(doc.get("rules"))
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if "*" in as_list(rule.get("verbs")):
            fields["rbac.rule.verbs=wildcard"] += 1
        if "*" in as_list(rule.get("resources")):
            fields["rbac.rule.resources=wildcard"] += 1
        if "*" in as_list(rule.get("apiGroups")):
            fields["rbac.rule.apiGroups=wildcard"] += 1


def analyse_binding(doc: dict, fields: Counter) -> None:
    """Count high-privilege bindings (cluster-admin, system:masters)."""
    role_ref = as_dict(doc.get("roleRef"))
    role_name = role_ref.get("name") or ""
    if role_name in {"cluster-admin", "system:masters"}:
        fields[f"binding.roleRef.name={role_name}"] += 1


def analyse_relationships(doc: dict, rels: Counter) -> None:
    """Detect inter-resource references and increment the relationship counters.

    References are identified by *encoding* (selector, name reference, owner
    reference, image string), not by *resolving* the targets. The point is
    to count which relationship types appear in the corpus, not to resolve
    the graph topologically.
    """
    kind = doc.get("kind")

    # Service -> Pod via label selector
    if kind == "Service" and safe_get(doc, "spec", "selector"):
        rels["Service->Pod (selector)"] += 1

    # Workload controller -> Pod via template + selector
    if kind in {"Deployment", "StatefulSet", "DaemonSet"} and safe_get(doc, "spec", "selector"):
        rels[f"{kind}->Pod (template+selector)"] += 1

    # NetworkPolicy -> Pod via podSelector
    if kind == "NetworkPolicy" and safe_get(doc, "spec", "podSelector") is not None:
        rels["NetworkPolicy->Pod (podSelector)"] += 1

    # Ingress -> Service via backend reference
    if kind == "Ingress":
        if as_list(safe_get(doc, "spec", "rules")) or safe_get(doc, "spec", "defaultBackend"):
            rels["Ingress->Service (backend)"] += 1

    # RoleBinding/ClusterRoleBinding -> Role + ServiceAccount
    if kind in {"RoleBinding", "ClusterRoleBinding"}:
        role_ref = as_dict(doc.get("roleRef"))
        if role_ref:
            target = role_ref.get("kind", "Role")
            rels[f"{kind}->{target} (roleRef)"] += 1
        for subj in as_list(doc.get("subjects")):
            if isinstance(subj, dict) and subj.get("kind") == "ServiceAccount":
                rels[f"{kind}->ServiceAccount (subjects)"] += 1
                break  # count once per binding, not per subject

    # Pod-spec references (workloads + Pods)
    if kind in WORKLOAD_KINDS:
        pod_spec = get_pod_spec(doc)
        if not pod_spec:
            return

        sa = pod_spec.get("serviceAccountName") or pod_spec.get("serviceAccount")
        if sa:
            rels["Pod->ServiceAccount (serviceAccountName)"] += 1

        # Volumes referencing Secrets, ConfigMaps, or PVCs.
        for vol in as_list(pod_spec.get("volumes")):
            if not isinstance(vol, dict):
                continue
            if "secret" in vol:
                rels["Pod->Secret (volume)"] += 1
            if "configMap" in vol:
                rels["Pod->ConfigMap (volume)"] += 1
            if "persistentVolumeClaim" in vol:
                rels["Pod->PersistentVolumeClaim (volume)"] += 1

        # envFrom + image references.
        containers = as_list(pod_spec.get("containers")) + as_list(pod_spec.get("initContainers"))
        for c in containers:
            if not isinstance(c, dict):
                continue
            for ef in as_list(c.get("envFrom")):
                if not isinstance(ef, dict):
                    continue
                if "secretRef" in ef:
                    rels["Pod->Secret (envFrom)"] += 1
                if "configMapRef" in ef:
                    rels["Pod->ConfigMap (envFrom)"] += 1
            if c.get("image"):
                rels["Pod->ContainerImage (image)"] += 1


def run_security(ds) -> dict:
    """Run the full security analysis pass over the dataset.

    Returns a dict suitable for JSON serialisation containing all counters
    and the parse-failure tally. Also writes the first 5 parse-failure
    examples to ``parse_failures.log`` for inspection.
    """
    fields_counter = Counter()
    rels_counter   = Counter()
    parse_failures = 0
    parse_examples: list[tuple[int, str]] = []
    counted_per_kind: Counter = Counter()

    print(f"Analysing {fmt(len(ds))} manifests... (this takes ~2 minutes)")

    for idx, row in enumerate(ds):
        content = row["content"]
        if not content:
            continue
        try:
            doc = yaml.safe_load(content)
        except yaml.YAMLError as e:
            parse_failures += 1
            if len(parse_examples) < 5:
                parse_examples.append((idx, str(e)[:200]))
            continue

        if not isinstance(doc, dict):
            continue

        kind = doc.get("kind")
        if not kind:
            continue
        counted_per_kind[kind] += 1

        if kind in WORKLOAD_KINDS:
            analyse_workload(doc, fields_counter)
        elif kind == "Service":
            analyse_service(doc, fields_counter)
        elif kind == "Ingress":
            analyse_ingress(doc, fields_counter)
        elif kind in {"Role", "ClusterRole"}:
            analyse_rbac(doc, fields_counter)
        elif kind in {"RoleBinding", "ClusterRoleBinding"}:
            analyse_binding(doc, fields_counter)

        analyse_relationships(doc, rels_counter)

        if (idx + 1) % 10000 == 0:
            print(f"  ... {fmt(idx + 1)} / {fmt(len(ds))} processed")

    if parse_examples:
        with PARSE_LOG.open("w") as fh:
            for i, msg in parse_examples:
                fh.write(f"row {i}: {msg}\n\n")

    print(f"\n  parse failures: {fmt(parse_failures)}")
    if parse_failures:
        print(f"  first 5 logged to {PARSE_LOG}")

    print_top(fields_counter, "Security-relevant fields", n=len(fields_counter))
    print_top(rels_counter, "Inter-resource relationships", n=len(rels_counter))

    return {
        "n_rows": len(ds),
        "parse_failures": parse_failures,
        "fields": dict(fields_counter),
        "relationships": dict(rels_counter),
        "kinds_counted": dict(counted_per_kind),
    }


# ---------------------------------------------------------------------------
# Mode 3: chart generation
# ---------------------------------------------------------------------------


def make_charts(ds, security_results: dict) -> None:
    """Generate three PNG bar charts under ``figures/``.

    Charts produced:

    * ``kinds_top20.png`` --- top 20 Kubernetes kinds by frequency.
    * ``security_fields.png`` --- security-field prevalence, grouped by category.
    * ``relationships.png`` --- inter-resource relationship counts.
    """
    import matplotlib.pyplot as plt

    FIGURES_DIR.mkdir(exist_ok=True)

    # --- Chart 1: top-20 kinds -------------------------------------------------
    kind_counts = Counter(ds["kind"]).most_common(20)
    labels  = [k for k, _ in kind_counts][::-1]
    values  = [v for _, v in kind_counts][::-1]

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.barh(labels, values, color="#0F2A47")
    ax.set_xlabel("Number of manifests")
    ax.set_title("Top 20 Kubernetes kinds in KubeObjects")
    for i, v in enumerate(values):
        ax.text(v, i, f" {fmt(v)}", va="center", fontsize=8)
    plt.tight_layout()
    plt.savefig(FIGURES_DIR / "kinds_top20.png", dpi=150)
    plt.close(fig)

    # --- Chart 2: security fields ---------------------------------------------
    fields = security_results["fields"]
    if fields:
        sorted_fields = sorted(fields.items(), key=lambda kv: kv[1], reverse=True)
        labels  = [k for k, _ in sorted_fields][::-1]
        values  = [v for _, v in sorted_fields][::-1]

        def colour_for(label: str) -> str:
            if label.startswith("pod.") or label.startswith("container."):
                return "#D97706"  # amber --- workload
            if label.startswith("service.") or label.startswith("ingress."):
                return "#0D9488"  # teal --- networking
            if label.startswith("rbac.") or label.startswith("binding."):
                return "#0F2A47"  # navy --- access control
            return "#475569"      # slate --- other

        colours = [colour_for(l) for l in labels]

        fig, ax = plt.subplots(figsize=(9, max(4, len(labels) * 0.3)))
        ax.barh(labels, values, color=colours)
        ax.set_xlabel("Number of manifests / containers")
        ax.set_title("Security-relevant fields in KubeObjects, by category")
        for i, v in enumerate(values):
            ax.text(v, i, f" {fmt(v)}", va="center", fontsize=8)
        plt.tight_layout()
        plt.savefig(FIGURES_DIR / "security_fields.png", dpi=150)
        plt.close(fig)

    # --- Chart 3: relationships -----------------------------------------------
    rels = security_results["relationships"]
    if rels:
        sorted_rels = sorted(rels.items(), key=lambda kv: kv[1], reverse=True)
        labels = [k for k, _ in sorted_rels][::-1]
        values = [v for _, v in sorted_rels][::-1]

        fig, ax = plt.subplots(figsize=(9, max(4, len(labels) * 0.3)))
        ax.barh(labels, values, color="#0D9488")
        ax.set_xlabel("Number of references")
        ax.set_title("Inter-resource relationship types in KubeObjects")
        for i, v in enumerate(values):
            ax.text(v, i, f" {fmt(v)}", va="center", fontsize=8)
        plt.tight_layout()
        plt.savefig(FIGURES_DIR / "relationships.png", dpi=150)
        plt.close(fig)

    print(f"\nCharts written to {FIGURES_DIR}/")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI flags and dispatch to the requested analysis mode(s)."""
    parser = argparse.ArgumentParser(description="Analyse the KubeObjects dataset.")
    parser.add_argument(
        "--security", action="store_true",
        help="Run security-relevant field analysis (slow; parses every manifest)."
    )
    parser.add_argument(
        "--charts", action="store_true",
        help="Generate PNG bar charts under figures/. Implies --security."
    )
    args = parser.parse_args()

    if args.charts:
        args.security = True

    if not ARROW_FILE.exists():
        print(f"ERROR: {ARROW_FILE} not found --- run 'git lfs pull'", file=sys.stderr)
        sys.exit(1)

    size_mb = ARROW_FILE.stat().st_size / 1024 / 1024
    print(f"Dataset dir  : {DATASET_DIR}")
    print(f"Arrow file   : {size_mb:.1f} MB")

    ds = load_from_disk(str(DATASET_DIR))

    run_overview(ds)

    if args.security:
        results = run_security(ds)
        OUTPUT_JSON.write_text(json.dumps(results, indent=2, sort_keys=True))
        print(f"\nResults written to {OUTPUT_JSON}")

        if args.charts:
            make_charts(ds, results)


if __name__ == "__main__":
    main()