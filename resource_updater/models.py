from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


@dataclass
class Recommendation:
    namespace: str
    deployment: str
    cpu_cur: str
    cpu_tgt: str
    mem_cur: str
    mem_tgt: str
    cpu_limit_cur: str
    mem_limit_cur: str
    cpu_limit_tgt: str
    mem_limit_tgt: str
    prom_cpu_p95: str = ""
    prom_mem_p95: str = ""
    prom_cpu_p99: str = ""
    prom_mem_p99: str = ""
    cpu_request_reason: str = ""
    cpu_limit_reason: str = ""
    mem_request_reason: str = ""
    mem_limit_reason: str = ""
    baseline_summary: str = ""
    sizing_action: str = ""


@dataclass(frozen=True)
class SkippedRecommendation:
    namespace: str
    deployment: str
    repo: str
    category: str
    reason: str


@dataclass
class PRData:
    repo: str
    repo_dir: Path
    base_branch: str
    work_branch: str
    pr_id: int
    pr_url: str
    auto_merge_safe: bool = True
    review_reasons: Tuple[str, ...] = ()
    deployments: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class BitbucketAuth:
    principal: str
    secret: str
    mode: str
