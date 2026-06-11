# Prometheus Resource Auto-Update

Automates Kubernetes resource right-sizing from Prometheus/Thanos usage data, updates Kustomize service manifests, and generates pull requests natively via the Bitbucket API.

For a web-friendly overview, see `docs/index.html` or view the hosted [GitHub Pages documentation](https://arogan178.github.io/prometheus-resource-auto-update/).

## Documentation

The project documentation is built as a static site located in the `docs/` folder.

### Deploying to GitHub Pages

We have included a GitHub Actions workflow that automates deployment of the documentation to GitHub Pages on every push to the `master` branch.

To set up and enable GitHub Pages for this repository:

1. Go to your repository settings on GitHub.
2. Select **Pages** from the sidebar.
3. Under **Build and deployment** -> **Source**, select **GitHub Actions** from the dropdown menu.
4. The workflow will automatically run on your next push, or you can trigger it manually under the **Actions** tab of the repository.

To view the documentation offline/locally, simply open `docs/index.html` in your web browser.

## Core Features

- **Automated Resource Tuning:** Applies Prometheus-based CPU and memory recommendations to `limits-patch.yaml` or relevant resource files across multiple repositories simultaneously.
- **Prometheus Integration:** Queries Prometheus/Thanos for usage percentiles and derives request/limit targets without relying on namespace labels or external recommender objects.
- **Interactive Review:** Provides an interactive CLI to review and approve changes before merging, with auto-merge capabilities for safe, below-threshold updates.
- **Revert Mode:** Rolls back recent automated optimizations to a previous stable state by detecting earlier resource-update commits and checking out pre-update file versions.

## Prerequisites

- Python 3.10+
- `kubectl` authenticated to the target cluster
- Bitbucket API token or app password (set in `.env`)

## Configuration (`.env`)

The script requires a `.env` file in the root directory. You can copy `.env.example` as a starting point.

```bash
# Bitbucket Authentication (Choose one pair)
BITBUCKET_EMAIL=you@example.com
BITBUCKET_API_TOKEN=<your-token>
# OR
# BITBUCKET_USERNAME=your_username
# BITBUCKET_APP_PASSWORD=your_app_password

# Organization Settings
BITBUCKET_WORKSPACE=my-company-workspace
REPO_PREFIX=

# Branch configuration
RESOURCE_UPDATE_BRANCH=resource-update
```

**Local repo paths** — update these to match where you have cloned the target repositories. `KUSTOMIZE_REPOS_DIR` can point either to one mono repo or to a parent directory containing many kustomize repos.

| Variable | Default | Description |
| --- | --- | --- |
| `KUSTOMIZE_REPOS_DIR` | `/path/to/kustomize-repos` | A single kustomize repo root, or a directory containing multiple kustomize repo checkouts |
| `KUSTOMIZE_MONOREPO_NAME` | basename of `KUSTOMIZE_REPOS_DIR` | Repo slug/name to use when `KUSTOMIZE_REPOS_DIR` points at one mono repo |
| `CLUSTER_CONFIG` | `/path/to/cluster-config-repo` | Cluster config repo |

**Cluster & Namespace Mapping:**

| Variable | Default | Description |
| --- | --- | --- |
| `CLUSTER_MAPPING` | `{}` | JSON map of env abbreviation to cluster name (e.g. `{"dev": "cluster-1"}`) |
| `NAMESPACE_INCLUDE_REGEX` | *(empty)* | Optional regex for filtering namespaces; empty scans every accessible namespace |
| `CLUSTER_CONFIG_APPS_TEMPLATE` | `namespaces/{cluster}/{namespace}/applications/kustomization.yaml` | Optional template for discovering app repos from a cluster-config checkout |
| `REPO_PREFIX` | *(empty)* | Optional Bitbucket repo slug prefix |
| `REPO_NAME_PREFIXES` | *(empty)* | Optional comma-separated prefixes stripped while mapping repo names |
| `REPO_ALIAS_PREFIXES` | *(empty)* | Optional comma-separated prefixes stripped while matching workload names to repos |

**Thresholds and Tuning Factors:**

| Variable | Default | Description |
| --- | --- | --- |
| `AUTO_MERGE_CPU_THRESHOLD` | `1` | CPU target above which PRs require manual review |
| `AUTO_MERGE_MEMORY_THRESHOLD` | `4Gi` | Memory target above which PRs require manual review |
| `CPU_REQUEST_TARGET_FACTOR` | `0.95` | Multiplier applied to base CPU targets to set requests |
| `MEMORY_REQUEST_TARGET_FACTOR` | `1.15` | Multiplier applied to base Memory targets to set requests |
| `CPU_LIMIT_BUFFER_FACTOR` | `1.20` | Multiplier applied to CPU limits |
| `MEMORY_LIMIT_BUFFER_FACTOR` | `1.20` | Multiplier applied to Memory limits |
| `MIN_CPU_TARGET_MILLIS` | `1` | Minimum CPU recommendation floor in millicores |
| `MIN_MEMORY_TARGET_MI` | `1` | Minimum memory recommendation floor in Mi |
| `BULK_REQUEST_MIN_RETAIN_FACTOR` | `0.25` | Minimum fraction of current requests kept in one bulk tuning run |
| `BULK_LIMIT_MIN_RETAIN_FACTOR` | `0.25` | Minimum fraction of current limits kept in one bulk tuning run |
| `IDLE_CPU_P95_THRESHOLD_MILLIS` | `10` | Workload is considered idle when CPU P95 is at or below this value |
| `IDLE_MEMORY_P95_THRESHOLD_MI` | `128` | Workload is considered idle when memory P95 is at or below this value |
| `IDLE_REQUEST_CPU_MILLIS` | `10` | Minimum CPU request for idle workloads |
| `IDLE_REQUEST_MEMORY_MI` | `128` | Minimum memory request for idle workloads |
| `IDLE_LIMIT_CPU_MILLIS` | `50` | Minimum CPU limit for idle workloads |
| `IDLE_LIMIT_MEMORY_MI` | `256` | Minimum memory limit for idle workloads |

**Prometheus Settings:**

| Variable | Default | Description |
| --- | --- | --- |
| `PROMETHEUS_URL` | *(auto)* | Prometheus/Thanos URL (auto-detected via `kubectl get route` if empty) |
| `PROMETHEUS_TOKEN` | *(auto)* | Bearer token (auto-detected via `oc whoami -t` if empty) |
| `PROMETHEUS_DAYS` | `30` | Lookback period for metric queries |
| `PROMETHEUS_PERCENTILE` | `0.95` | Percentile for request targets |
| `PROMETHEUS_LIMIT_PERCENTILE` | `0.99` | Percentile for limit targets |

## Usage

Run the tool from the command line:

```bash
python3 auto-update.py
```

The interactive wizard will guide you through the process:

1. **Authentication:** Validates Bitbucket credentials from `.env` or prompts for them.
2. **Cluster Check:** Verifies you are authenticated to the cluster.
3. **Dry-run Extraction:** Optionally infer environments from namespace suffixes and generate report files only. Namespaces ending in `-dev01` produce a `dev` report; namespaces ending in `-uat01`, `-stg01`, and `-prd01` produce separate CPE-style reports.
4. **Environment:** For the standard flow, enter the target environment or cluster alias (e.g., `dev`, `stg`, `prd`).
5. **Namespace Selection:** Select specific namespaces or process all discovered namespaces.
6. **Operation Mode:**
   - **[u] Update:** Fetch metrics and apply optimizations.
   - **[v] Revert:** Roll back the last optimization batch.
7. **PR Action:**
   - **[r] Review:** Interactively review PRs with diffs, merge, decline, or auto-merge safe ones.
   - **[l] Leave open:** Create PRs but do not review them.
   - **[f] Dry run:** Do not modify files or create PRs; only generate a detailed Markdown report.

## Sizing Policy

Resource limits are computed to establish a **Burstable QoS** class (where limits are higher than requests, allowing for burst activity).

**Target Calculation:**
The update flow requires Prometheus/Thanos usage data. Namespaces with no usable Prometheus data are skipped.

By default, request targets use 30-day P95 usage and limit targets use 30-day P99 usage:

- **CPU Request:** `P95 CPU * CPU_REQUEST_TARGET_FACTOR`
- **Memory Request:** `P95 Memory * MEMORY_REQUEST_TARGET_FACTOR`
- **CPU Limit:** `P99 CPU * CPU_LIMIT_BUFFER_FACTOR`
- **Memory Limit:** `P99 Memory * MEMORY_LIMIT_BUFFER_FACTOR`

Prometheus-derived values are rounded up before tightening so tiny workloads do not produce invalid `0` CPU or `0Mi` memory recommendations. Limits are also floored so they never drop below the corresponding request.

For safer bulk right-sizing, the tool also applies guardrails:

- **Per-run retain floors:** requests and limits will not be reduced below a configurable fraction of their current values in a single run.
- **Idle workload floors:** if a workload's Prometheus P95 CPU and memory are both below the idle thresholds, the tool enforces minimum request and limit values so intermittent or mostly idle services are not squeezed too aggressively.

## Relationship To KRR

[KRR](https://github.com/robusta-dev/krr) is a mature Kubernetes resource recommendation engine. This project is narrower: it focuses on turning Prometheus-derived recommendations into Kustomize patch updates and pull requests.

If you only need reports, dashboards, or generic recommendation output, KRR is likely the better fit. If you need an opinionated GitOps workflow that edits Kustomize repositories, opens PRs, supports dry-run reports, and can revert previous automated changes, this tool covers that apply/review layer. A future integration could consume KRR JSON output as the recommendation source.

## Outputs

In addition to interacting with Bitbucket, the script provides detailed console output during execution. If run in dry-run mode (`f`), it generates comprehensive Markdown reports outlining exactly what would change:

- `resource_changes_<env>_<date>.md`
- `resource_reverts_<env>_<date>.md`

These reports include aggregated cluster-wide savings and a detailed breakdown of per-deployment impact, sorting the most significant resource shifts to the top.

Multi-environment dry-run extraction writes one report per inferred environment under a timestamped run directory:

```text
resource_update_reports/index.html          # bookmark/share this entry point
resource_update_reports/<date_time>/index.html
resource_update_reports/<date_time>/
  resource_changes_uat.md
  resource_changes_uat.html
  resource_changes_stg.md
  resource_changes_stg.html
  resource_changes_prd.md
  resource_changes_prd.html
```

Each environment also gets a self-contained HTML dashboard with sortable tables and navigation back to the run and report home indexes.
