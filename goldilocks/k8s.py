import json
import math
import os
import ssl
import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, List, Dict, Tuple

from goldilocks.config import (
    PROMETHEUS_URL,
    PROMETHEUS_TOKEN,
    PROMETHEUS_VERIFY_SSL,
    PROMETHEUS_TIMEOUT,
    PROMETHEUS_DAYS,
    PROMETHEUS_PERCENTILE,
    PROMETHEUS_LIMIT_PERCENTILE,
    MAX_WORKERS_PROM,
    MIN_CPU_DIFF_MILLIS,
    MIN_MEM_DIFF_MI,
)
from goldilocks.models import Recommendation
from goldilocks.resources import (
    get_request_tightening_policy,
    get_limit_buffer_policy,
    get_bulk_retain_policy,
    get_idle_workload_policy,
    cpu_to_millis,
    memory_to_mi,
    tighten_cpu_request,
    tighten_memory_request,
    buffered_cpu_limit,
    buffered_memory_limit,
    convert_cpu,
    convert_memory,
    format_cpu_millis,
    ceil_cpu_cores_to_millis,
    ceil_memory_to_mi,
    format_memory_mi,
    retained_cpu_millis,
    retained_memory_mi,
)
from goldilocks.utils import run_cmd, log, log_warn

_prom_url_cache = None
_prom_token_cache = None


def get_cluster(env: str) -> str:
    # Example mapping: "dev" -> "cluster-1", "prd" -> "cluster-2"
    # By default, falls back to env if not defined
    mapping_str = os.getenv("CLUSTER_MAPPING", "{}")
    try:
        mapping = json.loads(mapping_str)
        return mapping.get(env, env)
    except json.JSONDecodeError:
        return env


def get_namespaces() -> List[str]:
    res = run_cmd(["oc", "get", "projects", "-o", "jsonpath={.items[*].metadata.name}"])
    if res.returncode != 0:
        return []
    return res.stdout.strip().split()


def get_prometheus_url(ns: str) -> Optional[str]:
    global _prom_url_cache
    if PROMETHEUS_URL:
        _prom_url_cache = PROMETHEUS_URL
        return PROMETHEUS_URL
    if _prom_url_cache:
        return _prom_url_cache
    res = run_cmd(
        [
            "oc",
            "get",
            "route",
            "thanos-querier",
            "-n",
            "openshift-monitoring",
            "-o",
            "jsonpath={.spec.host}",
        ]
    )
    if res.returncode != 0 or not res.stdout.strip():
        log_warn(
            "Could not detect Thanos Querier route. "
            "Set PROMETHEUS_URL in .env or ensure 'oc get route thanos-querier -n openshift-monitoring' works."
        )
        return None
    host = res.stdout.strip()
    _prom_url_cache = f"https://{host}"
    return _prom_url_cache


def get_prometheus_token() -> Optional[str]:
    global _prom_token_cache
    if PROMETHEUS_TOKEN:
        _prom_token_cache = PROMETHEUS_TOKEN
        return PROMETHEUS_TOKEN
    if _prom_token_cache:
        return _prom_token_cache
    res = run_cmd(["oc", "whoami", "-t"])
    if res.returncode != 0 or not res.stdout.strip():
        log_warn(
            "Could not get auth token. Run 'oc login' first or set PROMETHEUS_TOKEN in .env."
        )
        return None
    _prom_token_cache = res.stdout.strip()
    return _prom_token_cache


def query_prometheus(prom_url: str, token: str, query: str) -> Optional[list]:
    encoded = urllib.parse.urlencode({"query": query})
    url = f"{prom_url}/api/v1/query?{encoded}"
    verify_ssl = PROMETHEUS_VERIFY_SSL.lower() in {"1", "true", "yes", "y", "on"}
    timeout = int(PROMETHEUS_TIMEOUT)

    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")

    import time
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
                if body.get("status") == "success":
                    return body.get("data", {}).get("result", [])
                log_warn(f"Prometheus query returned status: {body.get('status')}")
                return None
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
            OSError,
        ) as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            log_warn(f"Prometheus query failed after retries: {e}")
            return None


def get_prometheus_resource_data(
    ns: str, prom_url: str, token: str
) -> Dict[str, Dict[str, float]]:
    days = PROMETHEUS_DAYS
    pct = PROMETHEUS_PERCENTILE
    lim_pct = PROMETHEUS_LIMIT_PERCENTILE

    # Requests: P95
    cpu_req_query = (
        f"max by (container) ("
        f"  quantile_over_time({pct}, rate(container_cpu_usage_seconds_total{{"
        f'    namespace="{ns}", container!="", container!="POD"'
        f"}}[5m])[{days}d:1h]))"
    )
    mem_req_query = (
        f"max by (container) ("
        f"  quantile_over_time({pct}, container_memory_working_set_bytes{{"
        f'    namespace="{ns}", container!="", container!="POD"'
        f"}}[{days}d:1h])) / 1048576"
    )

    # Limits: P99
    cpu_lim_query = (
        f"max by (container) ("
        f"  quantile_over_time({lim_pct}, rate(container_cpu_usage_seconds_total{{"
        f'    namespace="{ns}", container!="", container!="POD"'
        f"}}[5m])[{days}d:1h]))"
    )
    mem_lim_query = (
        f"max by (container) ("
        f"  quantile_over_time({lim_pct}, container_memory_working_set_bytes{{"
        f'    namespace="{ns}", container!="", container!="POD"'
        f"}}[{days}d:1h])) / 1048576"
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS_PROM) as executor:
        f_cpu_req = executor.submit(query_prometheus, prom_url, token, cpu_req_query)
        f_mem_req = executor.submit(query_prometheus, prom_url, token, mem_req_query)
        f_cpu_lim = executor.submit(query_prometheus, prom_url, token, cpu_lim_query)
        f_mem_lim = executor.submit(query_prometheus, prom_url, token, mem_lim_query)

        cpu_req_res = f_cpu_req.result()
        mem_req_res = f_mem_req.result()
        cpu_lim_res = f_cpu_lim.result()
        mem_lim_res = f_mem_lim.result()

    data: Dict[str, Dict[str, float]] = {}

    def merge_results(results, key):
        if results:
            for item in results:
                container = item.get("metric", {}).get("container", "")
                value = float(item.get("value", [0, "0"])[1])
                if container and value > 0:
                    data.setdefault(
                        container,
                        {
                            "cpu_req": 0.0,
                            "mem_req": 0.0,
                            "cpu_lim": 0.0,
                            "mem_lim": 0.0,
                        },
                    )
                    data[container][key] = round(value, 6 if "cpu" in key else 1)

    merge_results(cpu_req_res, "cpu_req")
    merge_results(mem_req_res, "mem_req")
    merge_results(cpu_lim_res, "cpu_lim")
    merge_results(mem_lim_res, "mem_lim")

    if data:
        log(f"Prometheus 30d: found usage data for {len(data)} containers in {ns}")
    else:
        log_warn(f"No Prometheus usage data found for {ns}.")

    return data


def get_prom_recommendations(ns: str) -> List[Recommendation]:
    recs = []
    cpu_factor, mem_factor = get_request_tightening_policy()
    cpu_limit_factor, mem_limit_factor = get_limit_buffer_policy()
    request_retain_factor, limit_retain_factor = get_bulk_retain_policy()
    (
        idle_cpu_threshold_millis,
        idle_memory_threshold_mi,
        idle_request_cpu_millis,
        idle_request_memory_mi,
        idle_limit_cpu_millis,
        idle_limit_memory_mi,
    ) = get_idle_workload_policy()

    deploy_res = run_cmd(
        ["oc", "get", "deployment,statefulset", "-n", ns, "-o", "json"]
    )
    if deploy_res.returncode != 0:
        log_warn(f"Failed to list deployments/statefulsets in {ns}")
        return []
    deploy_data = json.loads(deploy_res.stdout)

    prom_url = get_prometheus_url(ns)
    prom_token = get_prometheus_token()
    prom_data: Dict[str, Dict[str, float]] = {}
    if prom_url and prom_token:
        prom_data = get_prometheus_resource_data(ns, prom_url, prom_token)

    if not prom_data:
        log_warn(f"Skipping namespace {ns} because no Prometheus data was found.")
        return []

    for item in deploy_data.get("items", []):
        name = item.get("metadata", {}).get("name")
        if not name:
            continue

        try:
            containers = item["spec"]["template"]["spec"]["containers"]
            matched_container = None
            c_name = None
            for container in containers:
                c_name_tmp = container.get("name")
                if c_name_tmp in prom_data:
                    matched_container = container
                    c_name = c_name_tmp
                    break

            if not matched_container:
                if name in prom_data:
                    matched_container = containers[0]
                    c_name = name
                else:
                    continue

            resources = matched_container.get("resources", {})
            reqs = resources.get("requests", {})
            limits = resources.get("limits", {})

            cpu_cur = convert_cpu(reqs.get("cpu", "not set"))
            mem_cur_raw = reqs.get("memory", "")
            mem_cur = convert_memory(mem_cur_raw) if mem_cur_raw else "not set"
            cpu_limit_cur = convert_cpu(limits.get("cpu", "not set"))
            mem_limit_cur_raw = limits.get("memory", "")
            mem_limit_cur = (
                convert_memory(mem_limit_cur_raw) if mem_limit_cur_raw else "not set"
            )
        except (KeyError, IndexError):
            continue

        if not c_name:
            continue

        prom_vals = prom_data.get(c_name, {})
        if not prom_vals:
            continue

        prom_cpu_req = prom_vals.get("cpu_req", 0)
        prom_mem_req = prom_vals.get("mem_req", 0)
        prom_cpu_lim = prom_vals.get("cpu_lim", 0)
        prom_mem_lim = prom_vals.get("mem_lim", 0)

        if prom_cpu_req == 0 or prom_mem_req == 0:
            continue

        cpu_req_millis = ceil_cpu_cores_to_millis(prom_cpu_req)
        mem_req_mi = ceil_memory_to_mi(prom_mem_req)
        cpu_lim_millis = max(cpu_req_millis, ceil_cpu_cores_to_millis(prom_cpu_lim))
        mem_lim_mi = max(mem_req_mi, ceil_memory_to_mi(prom_mem_lim))

        cpu_tgt_raw = format_cpu_millis(cpu_req_millis)
        mem_tgt_raw = format_memory_mi(mem_req_mi)
        cpu_upper_raw = format_cpu_millis(cpu_lim_millis)
        mem_upper_raw = format_memory_mi(mem_lim_mi)

        prom_cpu_p95 = f"{prom_cpu_req:g}"
        prom_mem_p95 = format_memory_mi(int(math.ceil(prom_mem_req)))
        prom_cpu_p99 = f"{prom_cpu_lim:g}"
        prom_mem_p99 = format_memory_mi(int(math.ceil(prom_mem_lim)))

        cpu_tgt = tighten_cpu_request(cpu_tgt_raw, cpu_factor)
        mem_tgt = tighten_memory_request(mem_tgt_raw, mem_factor)

        cpu_limit_tgt = buffered_cpu_limit(cpu_upper_raw, cpu_limit_factor)
        mem_limit_tgt = buffered_memory_limit(mem_upper_raw, mem_limit_factor)

        c_cur_m = cpu_to_millis(cpu_cur) if cpu_cur != "not set" else 0
        c_tgt_m = cpu_to_millis(cpu_tgt) if cpu_tgt != "not set" else 0
        m_cur_mi = memory_to_mi(mem_cur) if mem_cur != "not set" else 0
        m_tgt_mi = memory_to_mi(mem_tgt) if mem_tgt != "not set" else 0
        lc_cur_m = cpu_to_millis(cpu_limit_cur) if cpu_limit_cur != "not set" else 0
        lc_tgt_m = cpu_to_millis(cpu_limit_tgt) if cpu_limit_tgt != "not set" else 0
        lm_cur_mi = memory_to_mi(mem_limit_cur) if mem_limit_cur != "not set" else 0
        lm_tgt_mi = memory_to_mi(mem_limit_tgt) if mem_limit_tgt != "not set" else 0

        c_cur_m, c_tgt_m = c_cur_m or 0, c_tgt_m or 0
        m_cur_mi, m_tgt_mi = m_cur_mi or 0, m_tgt_mi or 0
        lc_cur_m, lc_tgt_m = lc_cur_m or 0, lc_tgt_m or 0
        lm_cur_mi, lm_tgt_mi = lm_cur_mi or 0, lm_tgt_mi or 0

        is_idle_workload = (
            ceil_cpu_cores_to_millis(prom_cpu_req, minimum_millis=0)
            <= idle_cpu_threshold_millis
            and ceil_memory_to_mi(prom_mem_req, minimum_mi=0)
            <= idle_memory_threshold_mi
        )

        if is_idle_workload:
            c_tgt_m = max(c_tgt_m, idle_request_cpu_millis)
            m_tgt_mi = max(m_tgt_mi, idle_request_memory_mi)
            lc_tgt_m = max(lc_tgt_m, idle_limit_cpu_millis)
            lm_tgt_mi = max(lm_tgt_mi, idle_limit_memory_mi)

        c_tgt_m = max(c_tgt_m, retained_cpu_millis(c_cur_m, request_retain_factor))
        m_tgt_mi = max(
            m_tgt_mi, retained_memory_mi(m_cur_mi, request_retain_factor)
        )
        lc_tgt_m = max(lc_tgt_m, retained_cpu_millis(lc_cur_m, limit_retain_factor))
        lm_tgt_mi = max(
            lm_tgt_mi, retained_memory_mi(lm_cur_mi, limit_retain_factor)
        )

        lc_tgt_m = max(lc_tgt_m, c_tgt_m)
        lm_tgt_mi = max(lm_tgt_mi, m_tgt_mi)

        cpu_tgt = format_cpu_millis(c_tgt_m)
        mem_tgt = format_memory_mi(m_tgt_mi)
        cpu_limit_tgt = format_cpu_millis(lc_tgt_m)
        mem_limit_tgt = format_memory_mi(lm_tgt_mi)

        req_cpu_diff = abs(c_tgt_m - c_cur_m)
        req_mem_diff = abs(m_tgt_mi - m_cur_mi)
        lim_cpu_diff = abs(lc_tgt_m - lc_cur_m)
        lim_mem_diff = abs(lm_tgt_mi - lm_cur_mi)

        if (req_cpu_diff < MIN_CPU_DIFF_MILLIS and 
            lim_cpu_diff < MIN_CPU_DIFF_MILLIS and 
            req_mem_diff < MIN_MEM_DIFF_MI and 
            lim_mem_diff < MIN_MEM_DIFF_MI):
            continue

        recs.append(
            Recommendation(
                namespace=ns,
                deployment=name,
                cpu_cur=cpu_cur,
                cpu_tgt=cpu_tgt,
                mem_cur=mem_cur,
                mem_tgt=mem_tgt,
                cpu_limit_cur=cpu_limit_cur,
                mem_limit_cur=mem_limit_cur,
                cpu_limit_tgt=cpu_limit_tgt,
                mem_limit_tgt=mem_limit_tgt,
                prom_cpu_p95=prom_cpu_p95,
                prom_mem_p95=prom_mem_p95,
                prom_cpu_p99=prom_cpu_p99,
                prom_mem_p99=prom_mem_p99,
            )
        )

    return recs


def wait_for_rollouts(deployments: List[Tuple[str, str]]):
    from goldilocks.config import ROLLOUT_TIMEOUT
    from goldilocks.utils import Spinner, log_warn
    if not deployments:
        return
    for ns, deploy in deployments:
        with Spinner(f"Waiting for rollout of {deploy} in {ns} (timeout {ROLLOUT_TIMEOUT})..."):
            res = run_cmd(["oc", "rollout", "status", f"deploy/{deploy}", "-n", ns, f"--timeout={ROLLOUT_TIMEOUT}"], check=False)
            if "NotFound" in res.stderr or "NotFound" in res.stdout:
                res = run_cmd(["oc", "rollout", "status", f"sts/{deploy}", "-n", ns, f"--timeout={ROLLOUT_TIMEOUT}"], check=False)
            
        if res.returncode == 0:
            print(f"  [OK] Rollout complete for {deploy}")
        else:
            log_warn(f"Rollout wait failed or timed out for {deploy} in {ns}.")
