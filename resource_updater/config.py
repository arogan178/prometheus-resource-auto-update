import os
from datetime import date
from pathlib import Path


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# Load .env relative to the main execution directory (auto-update.py root)
load_dotenv_file(Path(__file__).resolve().parent.parent / ".env")

BRANCH = os.getenv("RESOURCE_UPDATE_BRANCH", "resource-update")
WORKSPACE = os.getenv("BITBUCKET_WORKSPACE", "my-company-workspace")
REPOS_DIR = Path(os.getenv("REPOS_DIR", "/tmp/resource-update-repos"))

KUSTOMIZE_REPOS_DIR = Path(
    os.getenv("KUSTOMIZE_REPOS_DIR", "/path/to/kustomize-repos")
)
KUSTOMIZE_MONOREPO_NAME = os.getenv("KUSTOMIZE_MONOREPO_NAME", KUSTOMIZE_REPOS_DIR.name)
CLUSTER_CONFIG = Path(os.getenv("CLUSTER_CONFIG", "/path/to/cluster-config-repo"))

AUTO_MERGE_CPU_THRESHOLD = os.getenv("AUTO_MERGE_CPU_THRESHOLD", "1")
AUTO_MERGE_MEMORY_THRESHOLD = os.getenv("AUTO_MERGE_MEMORY_THRESHOLD", "4Gi")
CPU_REQUEST_TARGET_FACTOR = os.getenv("CPU_REQUEST_TARGET_FACTOR", "0.95")
MEMORY_REQUEST_TARGET_FACTOR = os.getenv("MEMORY_REQUEST_TARGET_FACTOR", "1.15")
CPU_LIMIT_BUFFER_FACTOR = os.getenv("CPU_LIMIT_BUFFER_FACTOR", "1.20")
MEMORY_LIMIT_BUFFER_FACTOR = os.getenv("MEMORY_LIMIT_BUFFER_FACTOR", "1.20")
MIN_CPU_TARGET_MILLIS = int(os.getenv("MIN_CPU_TARGET_MILLIS", "1"))
MIN_MEMORY_TARGET_MI = int(os.getenv("MIN_MEMORY_TARGET_MI", "1"))
BULK_REQUEST_MIN_RETAIN_FACTOR = os.getenv("BULK_REQUEST_MIN_RETAIN_FACTOR", "0.25")
BULK_LIMIT_MIN_RETAIN_FACTOR = os.getenv("BULK_LIMIT_MIN_RETAIN_FACTOR", "0.25")
IDLE_CPU_P95_THRESHOLD_MILLIS = int(os.getenv("IDLE_CPU_P95_THRESHOLD_MILLIS", "10"))
IDLE_MEMORY_P95_THRESHOLD_MI = int(os.getenv("IDLE_MEMORY_P95_THRESHOLD_MI", "128"))
IDLE_REQUEST_CPU_MILLIS = int(os.getenv("IDLE_REQUEST_CPU_MILLIS", "10"))
IDLE_REQUEST_MEMORY_MI = int(os.getenv("IDLE_REQUEST_MEMORY_MI", "128"))
IDLE_LIMIT_CPU_MILLIS = int(os.getenv("IDLE_LIMIT_CPU_MILLIS", "50"))
IDLE_LIMIT_MEMORY_MI = int(os.getenv("IDLE_LIMIT_MEMORY_MI", "256"))

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "").strip()
PROMETHEUS_TOKEN = os.getenv("PROMETHEUS_TOKEN", "").strip()
PROMETHEUS_DAYS = os.getenv("PROMETHEUS_DAYS", "30")
PROMETHEUS_PERCENTILE = os.getenv("PROMETHEUS_PERCENTILE", "0.95")
PROMETHEUS_LIMIT_PERCENTILE = os.getenv("PROMETHEUS_LIMIT_PERCENTILE", "0.99")
PROMETHEUS_VERIFY_SSL = os.getenv("PROMETHEUS_VERIFY_SSL", "false")
PROMETHEUS_TIMEOUT = os.getenv("PROMETHEUS_TIMEOUT", "120")

MIN_CPU_DIFF_MILLIS = int(os.getenv("MIN_CPU_DIFF_MILLIS", "50"))
MIN_MEM_DIFF_MI = int(os.getenv("MIN_MEM_DIFF_MI", "128"))
WAIT_FOR_ROLLOUT = os.getenv("WAIT_FOR_ROLLOUT", "true").lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)
ROLLOUT_TIMEOUT = os.getenv("ROLLOUT_TIMEOUT", "5m")

MAX_WORKERS_REPOS = int(os.getenv("MAX_WORKERS_REPOS", "8"))
MAX_WORKERS_PROM = int(os.getenv("MAX_WORKERS_PROM", "4"))
DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes", "on")

UPDATE_COMMIT_MSG = f"{BRANCH} Update resources based on Prometheus metrics"
REVERT_COMMIT_MSG = f"{BRANCH} Revert Prometheus resource optimizations"

TODAY = date.today().isoformat()
