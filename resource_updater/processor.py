import os
import sys
import urllib.error
import concurrent.futures
from typing import List, Tuple, Optional, Dict
from pathlib import Path

from resource_updater.models import Recommendation, PRData, SkippedRecommendation
from resource_updater.config import (
    BRANCH,
    UPDATE_COMMIT_MSG,
    REVERT_COMMIT_MSG,
    PROMETHEUS_DAYS,
    PROMETHEUS_PERCENTILE,
    DEBUG,
)
from resource_updater.utils import (
    log_warn,
    run_cmd,
    read_single_key,
    print_table,
    BLUE,
    YELLOW,
    RED,
    CYAN,
    GREEN,
    BOLD,
    NC,
)
from resource_updater.resources import (
    get_auto_merge_thresholds,
    get_limit_buffer_policy,
    get_request_tightening_policy,
    cpu_to_millis,
    memory_to_mi,
    format_memory_mi,
    format_val,
    format_diff_detailed,
)
from resource_updater.git import (
    get_repo,
    commit_push,
    find_resource_update_restore_source,
    describe_commit,
)
from resource_updater.kustomize import (
    update_limits,
    find_repo_env_dir,
    find_limits_file_for_deployment,
    find_resource_patch_file,
    read_limits_file,
)
from resource_updater.k8s import get_cluster
from resource_updater.bitbucket import (
    create_pull_request,
    merge_pull_request,
    decline_pull_request,
)


def generate_markdown_report(
    output_file: Path,
    env: str,
    title: str,
    applied_recs: List[Recommendation],
    prometheus_days: str,
    prometheus_percentile: str,
    skipped_recs: Optional[List[SkippedRecommendation]] = None,
):
    cpu_factor, mem_factor = get_request_tightening_policy()
    cpu_limit_factor, mem_limit_factor = get_limit_buffer_policy()
    cpu_pct = int(cpu_factor * 100)
    mem_pct = int(mem_factor * 100)
    cpu_lim_pct = int(cpu_limit_factor * 100)
    mem_lim_pct = int(mem_limit_factor * 100)
    skipped_recs = skipped_recs or []

    def md_cell(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", "<br>")

    def get_req_label(pct):
        if pct < 100:
            return f"Tightened {pct}%"
        elif pct == 100:
            return "Maintained 100%"
        else:
            return f"Buffered {pct}%"

    cpu_req_label = get_req_label(cpu_pct)
    mem_req_label = get_req_label(mem_pct)

    with output_file.open("w", encoding="utf-8") as f:
        f.write(f"# {title}\n")
        f.write(
            f"**Generated:** {len(applied_recs)} patchable deployment(s) across {len(set(r.namespace for r in applied_recs))} namespace(s)"
        )
        if skipped_recs:
            f.write(f"; {len(skipped_recs)} skipped/unpatchable recommendation(s)")
        f.write("\n\n")

        deployment_records = []
        ns_stats = {}
        total_cur_cpu, total_tgt_cpu, total_cur_mem, total_tgt_mem = 0, 0, 0, 0
        (
            total_cur_limit_cpu,
            total_tgt_limit_cpu,
            total_cur_limit_mem,
            total_tgt_limit_mem,
        ) = 0, 0, 0, 0

        for rec in applied_recs:
            c_cur = cpu_to_millis(rec.cpu_cur) or 0
            c_tgt = cpu_to_millis(rec.cpu_tgt) or 0
            m_cur = memory_to_mi(rec.mem_cur) or 0
            m_tgt = memory_to_mi(rec.mem_tgt) or 0
            lc_cur = cpu_to_millis(rec.cpu_limit_cur) or 0
            lc_tgt = cpu_to_millis(rec.cpu_limit_tgt) or 0
            lm_cur = memory_to_mi(rec.mem_limit_cur) or 0
            lm_tgt = memory_to_mi(rec.mem_limit_tgt) or 0

            cpu_diff = abs(c_tgt - c_cur)
            mem_diff = abs(m_tgt - m_cur)
            limit_cpu_diff = abs(lc_tgt - lc_cur)
            limit_mem_diff = abs(lm_tgt - lm_cur)
            total_impact = (
                (cpu_diff + limit_cpu_diff) * 1.024 + mem_diff + limit_mem_diff
            )
            deployment_records.append(
                (
                    total_impact,
                    rec,
                    c_cur,
                    c_tgt,
                    m_cur,
                    m_tgt,
                    lc_cur,
                    lc_tgt,
                    lm_cur,
                    lm_tgt,
                )
            )

            if rec.namespace not in ns_stats:
                ns_stats[rec.namespace] = {
                    "cur_cpu": 0,
                    "tgt_cpu": 0,
                    "cur_mem": 0,
                    "tgt_mem": 0,
                    "cur_limit_cpu": 0,
                    "tgt_limit_cpu": 0,
                    "cur_limit_mem": 0,
                    "tgt_limit_mem": 0,
                    "count": 0,
                }

            ns_stats[rec.namespace]["cur_cpu"] += c_cur
            ns_stats[rec.namespace]["tgt_cpu"] += c_tgt
            ns_stats[rec.namespace]["cur_mem"] += m_cur
            ns_stats[rec.namespace]["tgt_mem"] += m_tgt
            ns_stats[rec.namespace]["cur_limit_cpu"] += lc_cur
            ns_stats[rec.namespace]["tgt_limit_cpu"] += lc_tgt
            ns_stats[rec.namespace]["cur_limit_mem"] += lm_cur
            ns_stats[rec.namespace]["tgt_limit_mem"] += lm_tgt
            ns_stats[rec.namespace]["count"] += 1

            total_cur_cpu += c_cur
            total_tgt_cpu += c_tgt
            total_cur_mem += m_cur
            total_tgt_mem += m_tgt
            total_cur_limit_cpu += lc_cur
            total_tgt_limit_cpu += lc_tgt
            total_cur_limit_mem += lm_cur
            total_tgt_limit_mem += lm_tgt

        def diff_text(current: int, target: int, unit: str) -> str:
            return format_diff_detailed(current, target, unit).split(" ", 3)[-1]

        def change_text(current: int, target: int, unit: str) -> str:
            return (
                f"{format_val(current, unit)} -> {format_val(target, unit)} "
                f"{diff_text(current, target, unit)}"
            )

        def cell(current: int, target: int, unit: str) -> str:
            return f"{format_val(current, unit)} -> {format_val(target, unit)}"

        def note_lines(rec: Recommendation) -> List[str]:
            notes = []
            if rec.baseline_summary:
                notes.append(rec.baseline_summary)
            if rec.cpu_request_reason:
                notes.append(f"CPU request: {rec.cpu_request_reason}")
            if rec.cpu_limit_reason:
                notes.append(f"CPU limit: {rec.cpu_limit_reason}")
            if rec.mem_request_reason:
                notes.append(f"Memory request: {rec.mem_request_reason}")
            if rec.mem_limit_reason:
                notes.append(f"Memory limit: {rec.mem_limit_reason}")
            return notes

        prom_pct_display = int(float(prometheus_percentile) * 100)

        f.write("## Totals (All Namespaces in this Environment)\n")
        f.write("| Resource | Current | Target | Difference |\n")
        f.write("|---|---|---|---|\n")
        f.write(
            f"| CPU Requests | {format_val(total_cur_cpu, 'm')} | {format_val(total_tgt_cpu, 'm')} | {diff_text(total_cur_cpu, total_tgt_cpu, 'm')} |\n"
        )
        f.write(
            f"| Mem Requests | {format_val(total_cur_mem, 'Mi')} | {format_val(total_tgt_mem, 'Mi')} | {diff_text(total_cur_mem, total_tgt_mem, 'Mi')} |\n"
        )
        f.write(
            f"| CPU Limits | {format_val(total_cur_limit_cpu, 'm')} | {format_val(total_tgt_limit_cpu, 'm')} | {diff_text(total_cur_limit_cpu, total_tgt_limit_cpu, 'm')} |\n"
        )
        f.write(
            f"| Mem Limits | {format_val(total_cur_limit_mem, 'Mi')} | {format_val(total_tgt_limit_mem, 'Mi')} | {diff_text(total_cur_limit_mem, total_tgt_limit_mem, 'Mi')} |\n\n"
        )

        f.write("## Namespace Impact\n")
        sorted_ns = sorted(
            ns_stats.items(),
            key=lambda x: (
                (
                    abs(x[1]["tgt_cpu"] - x[1]["cur_cpu"])
                    + abs(x[1]["tgt_limit_cpu"] - x[1]["cur_limit_cpu"])
                )
                * 1.024
                + abs(x[1]["tgt_mem"] - x[1]["cur_mem"])
                + abs(x[1]["tgt_limit_mem"] - x[1]["cur_limit_mem"])
            ),
            reverse=True,
        )
        f.write("| Namespace | Deployments | CPU Req | Mem Req | CPU Limit | Mem Limit |\n")
        f.write("|---|---:|---|---|---|---|\n")
        for ns, stats in sorted_ns:
            f.write(
                f"| `{md_cell(ns)}` | {stats['count']} | "
                f"{change_text(stats['cur_cpu'], stats['tgt_cpu'], 'm')} | "
                f"{change_text(stats['cur_mem'], stats['tgt_mem'], 'Mi')} | "
                f"{change_text(stats['cur_limit_cpu'], stats['tgt_limit_cpu'], 'm')} | "
                f"{change_text(stats['cur_limit_mem'], stats['tgt_limit_mem'], 'Mi')} |\n"
            )
        f.write("\n")

        deployment_records.sort(key=lambda x: x[0], reverse=True)
        f.write("## Patchable Changes\n\n")
        f.write(
            "Sorted by total absolute CPU and memory movement. "
            f"Request policy: CPU {cpu_req_label}, memory {mem_req_label}; "
            f"limit buffers: CPU {cpu_lim_pct}%, memory {mem_lim_pct}%.\n\n"
        )
        f.write(
            "| Namespace | Deployment | Action | CPU Req | Mem Req | CPU Limit | Mem Limit |\n"
        )
        f.write("|---|---|---|---|---|---|---|\n")
        for (
            _impact,
            rec,
            c_cur,
            c_tgt,
            m_cur,
            m_tgt,
            lc_cur,
            lc_tgt,
            lm_cur,
            lm_tgt,
        ) in deployment_records:
            f.write(
                f"| `{md_cell(rec.namespace)}` | `{md_cell(rec.deployment)}` | "
                f"{md_cell(rec.sizing_action or 'change')} | "
                f"{cell(c_cur, c_tgt, 'm')} | "
                f"{cell(m_cur, m_tgt, 'Mi')} | "
                f"{cell(lc_cur, lc_tgt, 'm')} | "
                f"{cell(lm_cur, lm_tgt, 'Mi')} |\n"
            )
        f.write("\n")

        f.write("## Decision Details\n\n")
        f.write(
            f"<details>\n<summary>Prometheus signals and policy reasons "
            f"({len(deployment_records)} deployment(s))</summary>\n\n"
        )
        for (
            _impact,
            rec,
            *_values,
        ) in deployment_records:
            f.write(f"**`{rec.namespace} / {rec.deployment}`**\n\n")
            f.write(
                f"- Prometheus {prometheus_days}d P{prom_pct_display}: "
                f"CPU `{rec.prom_cpu_p95 or 'n/a'}`, memory `{rec.prom_mem_p95 or 'n/a'}`\n"
            )
            f.write(
                f"- Prometheus {prometheus_days}d limit pctl: "
                f"CPU `{rec.prom_cpu_p99 or 'n/a'}`, memory `{rec.prom_mem_p99 or 'n/a'}`\n"
            )
            for note in note_lines(rec):
                f.write(f"- {note}\n")
            f.write("\n")
        f.write("</details>\n\n")

        f.write("## Skipped / Unpatchable Recommendations\n\n")
        if not skipped_recs:
            f.write("No recommendations were skipped during dry-run validation.\n")
            return

        category_counts: Dict[str, int] = {}
        for skipped in skipped_recs:
            category_counts[skipped.category] = category_counts.get(skipped.category, 0) + 1

        f.write("| Category | Count |\n")
        f.write("|---|---:|\n")
        for category, count in sorted(category_counts.items()):
            f.write(f"| {md_cell(category)} | {count} |\n")

        f.write("\n| Namespace | Deployment | Repo | Category | Reason |\n")
        f.write("|---|---|---|---|---|\n")
        for skipped in sorted(
            skipped_recs,
            key=lambda item: (
                item.category,
                item.namespace,
                item.deployment,
                item.repo,
            ),
        ):
            f.write(
                f"| `{md_cell(skipped.namespace)}` | `{md_cell(skipped.deployment)}` | "
                f"`{md_cell(skipped.repo)}` | {md_cell(skipped.category)} | {md_cell(skipped.reason)} |\n"
            )


def get_manual_review_reasons(rec: Recommendation) -> List[str]:
    reasons: List[str] = []
    cpu_threshold, mem_threshold = get_auto_merge_thresholds()

    request_cpu = cpu_to_millis(rec.cpu_tgt) or 0
    request_mem = memory_to_mi(rec.mem_tgt) or 0

    if request_cpu > cpu_threshold:
        reasons.append(
            f"request CPU {rec.cpu_tgt} exceeds auto-merge threshold {format_val(cpu_threshold, 'm')}"
        )
    if request_mem > mem_threshold:
        reasons.append(
            f"request memory {rec.mem_tgt} exceeds auto-merge threshold {format_memory_mi(mem_threshold)}"
        )

    return reasons


def process_single_repo(
    repo: str, repo_recs: List[Recommendation], env: str, pr_action: str
) -> Tuple[List[str], int, int, Optional[PRData], List[Recommendation]]:
    logs = []
    logs.append(f"{BLUE}[CHANGE]{NC} Repo: {repo} ({len(repo_recs)} deployment(s))")

    u_count = 0
    f_count = 0
    pr_data = None
    applied_recs: List[Recommendation] = []
    review_reasons: List[str] = []

    if pr_action == "f":
        for rec in repo_recs:
            logs.append(f"  Namespace: {rec.namespace}, Deployment: {rec.deployment}")
            logs.append(f"    CPU request: {rec.cpu_cur} -> {rec.cpu_tgt}")
            logs.append(f"    Memory request: {rec.mem_cur} -> {rec.mem_tgt}")
            logs.append(
                f"    CPU limit (Burstable QoS): {rec.cpu_limit_cur} -> {rec.cpu_limit_tgt}"
            )
            logs.append(
                f"    Memory limit (Burstable QoS): {rec.mem_limit_cur} -> {rec.mem_limit_tgt}"
            )
            if rec.prom_cpu_p95 or rec.prom_mem_p95:
                logs.append(
                    f"    Prometheus {PROMETHEUS_DAYS}d P{int(float(PROMETHEUS_PERCENTILE) * 100)}: CPU={rec.prom_cpu_p95 or 'n/a'} cores, Memory={rec.prom_mem_p95 or 'n/a'}"
                )
            if rec.prom_cpu_p99 or rec.prom_mem_p99:
                logs.append(
                    f"    Prometheus {PROMETHEUS_DAYS}d Limit Pctl: CPU={rec.prom_cpu_p99 or 'n/a'} cores, Memory={rec.prom_mem_p99 or 'n/a'}"
                )
        logs.append("  [INFO] Dry run: No files changed, no PRs created.")
        return logs, 0, 0, None, repo_recs

    update_msg = f"{UPDATE_COMMIT_MSG} ({env.upper()})"
    work_branch = f"{BRANCH}-{env}"

    try:
        repo_dir, base_branch = get_repo(repo, work_branch)

        for rec in repo_recs:
            logs.append(f"  Namespace: {rec.namespace}, Deployment: {rec.deployment}")
            logs.append(f"    CPU request: {rec.cpu_cur} -> {rec.cpu_tgt}")
            logs.append(f"    Memory request: {rec.mem_cur} -> {rec.mem_tgt}")
            logs.append(
                f"    CPU limit (Burstable QoS): {rec.cpu_limit_cur} -> {rec.cpu_limit_tgt}"
            )
            logs.append(
                f"    Memory limit (Burstable QoS): {rec.mem_limit_cur} -> {rec.mem_limit_tgt}"
            )
            if rec.prom_cpu_p95 or rec.prom_mem_p95:
                logs.append(
                    f"    Prometheus {PROMETHEUS_DAYS}d P{int(float(PROMETHEUS_PERCENTILE) * 100)}: CPU={rec.prom_cpu_p95 or 'n/a'} cores, Memory={rec.prom_mem_p95 or 'n/a'}"
                )
            if rec.prom_cpu_p99 or rec.prom_mem_p99:
                logs.append(
                    f"    Prometheus {PROMETHEUS_DAYS}d Limit Pctl: CPU={rec.prom_cpu_p99 or 'n/a'} cores, Memory={rec.prom_mem_p99 or 'n/a'}"
                )
            review_reasons.extend(get_manual_review_reasons(rec))

            result = update_limits(
                repo,
                rec.namespace,
                rec.deployment,
                rec.cpu_tgt,
                rec.mem_tgt,
                rec.cpu_limit_tgt,
                rec.mem_limit_tgt,
                get_cluster(env),
            )
            logs.append(f"    Updated: {result}")

        if commit_push(repo_dir, work_branch, update_msg):
            u_count += 1
            applied_recs = list(repo_recs)
            logs.append("  [OK] Committed and pushed")

            pr_info = create_pull_request(
                repo,
                base_branch,
                work_branch,
                title=update_msg,
                desc="Automated resource updates based on Prometheus usage recommendations.",
            )
            if pr_info:
                pr_id, pr_url = pr_info
                unique_reasons = tuple(dict.fromkeys(review_reasons))
                auto_merge_safe = len(unique_reasons) == 0
                if auto_merge_safe:
                    logs.append("  [OK] Auto-merge eligible")
                else:
                    logs.append("  [WARN] Manual review required")
                    for reason in unique_reasons:
                        logs.append(f"    - {reason}")
                logs.append("  [OK] Pull request created or updated")
                pr_data = PRData(
                    repo,
                    repo_dir,
                    base_branch,
                    work_branch,
                    pr_id,
                    pr_url,
                    auto_merge_safe=auto_merge_safe,
                    review_reasons=unique_reasons,
                    deployments=tuple(
                        set((r.namespace, r.deployment) for r in applied_recs)
                    ),
                )
            else:
                logs.append(
                    f"  {YELLOW}[WARN]{NC} Pull request creation failed for repo: {repo}"
                )
        else:
            logs.append(f"  {YELLOW}[WARN]{NC} No changes or push failed")
    except Exception as e:
        import traceback

        if DEBUG:
            logs.append(
                f"  {RED}[ERROR]{NC} Traceback for {repo}:\n{traceback.format_exc()}"
            )
        logs.append(f"  {YELLOW}[WARN]{NC} Error processing {repo}: {e}")
        f_count += 1

    return logs, u_count, f_count, pr_data, applied_recs


def process_revert_single_repo(
    repo: str, ns_deploy_list: List[Tuple[str, str]], env: str, pr_action: str
) -> Tuple[List[str], int, int, Optional[PRData], List[Recommendation]]:
    logs = []
    logs.append(f"{BLUE}[REVERT]{NC} Repo: {repo}")

    u_count = 0
    f_count = 0
    pr_data = None
    work_branch = f"{BRANCH}-revert-{env}"
    applied_recs: List[Recommendation] = []
    cluster = get_cluster(env)

    try:
        repo_dir, base_branch = get_repo(repo, work_branch)
        current_states: Dict[Tuple[str, str], Tuple[str, str, str, str]] = {}
        restore_plan: Dict[Tuple[str, str], Tuple[str, str, str, str]] = {}
        restore_sources_by_file: Dict[str, str] = {}

        for ns, deploy in ns_deploy_list:
            if deploy == "*":
                for kustomization in repo_dir.rglob("kustomization.yaml"):
                    parts = kustomization.parts
                    if cluster not in parts or ns not in parts:
                        continue

                    env_dir = kustomization.parent
                    limits_file = find_resource_patch_file(env_dir)
                    if not limits_file:
                        continue

                    workload_label = env_dir.name
                    relative_path = limits_file.relative_to(repo_dir).as_posix()
                    current_states[(ns, workload_label)] = read_limits_file(
                        limits_file
                    )

                    (
                        _history_ref,
                        restore_source,
                        restore_display,
                        update_commits_found,
                    ) = find_resource_update_restore_source(
                        repo_dir, base_branch, relative_path, env
                    )
                    if (
                        update_commits_found == 0
                        or restore_source is None
                        or restore_display is None
                    ):
                        continue
                    restore_sources_by_file[relative_path] = restore_source
                    logs.append(
                        f"  [INFO] {relative_path}: {update_commits_found} resource update commit(s), restore to {describe_commit(repo_dir, restore_source)}"
                    )
                    restore_plan[(ns, workload_label)] = (
                        relative_path,
                        restore_source,
                        restore_display,
                        _history_ref,
                    )
                continue

            env_dir = find_repo_env_dir(repo_dir, cluster, ns, deploy)
            if not env_dir:
                logs.append(
                    f"  {YELLOW}[WARN]{NC} Could not find kustomization dir for {ns}/{deploy}."
                )
                continue
            limits_file = find_limits_file_for_deployment(env_dir, deploy)
            if not limits_file:
                logs.append(
                    f"  {YELLOW}[WARN]{NC} No resource patch file found for {ns}/{deploy}."
                )
                continue

            relative_path = limits_file.relative_to(repo_dir).as_posix()
            current_states[(ns, deploy)] = read_limits_file(limits_file)
            history_ref, restore_source, restore_display, update_commits_found = (
                find_resource_update_restore_source(
                    repo_dir, base_branch, relative_path, env
                )
            )

            if update_commits_found == 0:
                logs.append(
                    f"  {YELLOW}[WARN]{NC} No recent resource update commits found for {relative_path} on {history_ref}."
                )
                continue
            if restore_source is None or restore_display is None:
                logs.append(
                    f"  {YELLOW}[WARN]{NC} Could not determine a pre-update restore point for {relative_path}."
                )
                continue

            existing_restore_source = restore_sources_by_file.get(relative_path)
            if (
                existing_restore_source is not None
                and existing_restore_source != restore_source
            ):
                logs.append(
                    f"  {YELLOW}[WARN]{NC} Conflicting restore sources detected for {relative_path}."
                )
                continue

            restore_sources_by_file[relative_path] = restore_source
            logs.append(
                f"  [INFO] {relative_path}: {update_commits_found} resource update commit(s) on {history_ref}, restore to {describe_commit(repo_dir, restore_source)}"
            )
            restore_plan[(ns, deploy)] = (
                relative_path,
                restore_source,
                restore_display,
                history_ref,
            )

        if not restore_plan:
            return logs, 0, 0, None, []

        restored_files = set()
        for (
            relative_path,
            restore_source,
            restore_display,
            _history_ref,
        ) in restore_plan.values():
            if relative_path in restored_files:
                continue
            restore_res = run_cmd(
                ["git", "checkout", restore_source, "--", relative_path],
                cwd=repo_dir,
                check=False,
            )
            if restore_res.returncode != 0:
                logs.append(
                    f"  {YELLOW}[WARN]{NC} Failed to checkout {relative_path} from restore point {restore_display}."
                )
                return logs, 0, 1, None, []
            restored_files.add(relative_path)

        for (ns, deploy), (
            relative_path,
            _restore_source,
            _restore_display,
            _history_ref,
        ) in restore_plan.items():
            reverted_states = read_limits_file(repo_dir / relative_path)
            c_req_c, c_req_m, c_lim_c, c_lim_m = current_states[(ns, deploy)]
            r_req_c, r_req_m, r_lim_c, r_lim_m = reverted_states

            if (c_req_c, c_req_m, c_lim_c, c_lim_m) != (
                r_req_c,
                r_req_m,
                r_lim_c,
                r_lim_m,
            ):
                applied_recs.append(
                    Recommendation(
                        namespace=ns,
                        deployment=deploy,
                        cpu_cur=c_req_c,
                        cpu_tgt=r_req_c,
                        mem_cur=c_req_m,
                        mem_tgt=r_req_m,
                        cpu_limit_cur=c_lim_c,
                        mem_limit_cur=c_lim_m,
                        cpu_limit_tgt=r_lim_c,
                        mem_limit_tgt=r_lim_m,
                    )
                )

        if not applied_recs:
            logs.append(
                "  [INFO] Revert resulted in no resource changes. (Already reverted?)"
            )
            if pr_action == "f":
                return logs, 0, 0, None, []

        if pr_action == "f":
            restore_points = ", ".join(
                sorted(
                    {
                        restore_display
                        for _, _, restore_display, _ in restore_plan.values()
                    }
                )
            )
            logs.append(
                f"  [INFO] Dry run: Would restore {env.upper()} resources using pre-update commit(s): {restore_points}."
            )
            return logs, 1, 0, None, applied_recs

        if (
            run_cmd(["git", "diff", "--cached", "--quiet"], cwd=repo_dir).returncode
            == 0
            and run_cmd(["git", "diff", "--quiet"], cwd=repo_dir).returncode == 0
        ):
            logs.append("  [INFO] Revert resulted in no git diff changes.")
            return logs, 0, 0, None, []

        revert_msg = f"{REVERT_COMMIT_MSG} ({env.upper()})"
        if commit_push(repo_dir, work_branch, revert_msg):
            u_count += 1
            logs.append("  [OK] Revert committed and pushed")
            restore_points = ", ".join(
                sorted(
                    {
                        restore_display
                        for _, _, restore_display, _ in restore_plan.values()
                    }
                )
            )
            pr_info = create_pull_request(
                repo,
                base_branch,
                work_branch,
                title=revert_msg,
                desc=f"Reverting previous Prometheus resource updates for {env.upper()}. Restoring state from pre-update commit(s): {restore_points}.",
            )
            if pr_info:
                pr_id, pr_url = pr_info
                logs.append("  [OK] Revert Pull Request created")
                pr_data = PRData(
                    repo,
                    repo_dir,
                    base_branch,
                    work_branch,
                    pr_id,
                    pr_url,
                    auto_merge_safe=True,
                    review_reasons=(),
                    deployments=tuple(
                        set((r.namespace, r.deployment) for r in applied_recs)
                    ),
                )
            else:
                logs.append(
                    f"  {YELLOW}[WARN]{NC} Pull request creation failed for repo: {repo}"
                )
        else:
            logs.append(f"  {YELLOW}[WARN]{NC} Push failed after reverting.")
    except Exception as e:
        import traceback

        if DEBUG:
            logs.append(
                f"  {RED}[ERROR]{NC} Traceback for {repo}:\n{traceback.format_exc()}"
            )
        logs.append(f"  {YELLOW}[WARN]{NC} Error reverting {repo}: {e}")
        f_count += 1

    return logs, u_count, f_count, pr_data, applied_recs


def process_bulk_prs(action_name: str, pr_list: List[PRData], task_function):
    from resource_updater.config import WAIT_FOR_ROLLOUT
    from resource_updater.k8s import wait_for_rollouts

    is_merge = task_function.__name__ == "merge_pull_request"

    if is_merge and WAIT_FOR_ROLLOUT:
        print(
            f"\n[INFO] {action_name} remaining PRs sequentially to prevent rollout storms..."
        )
        for pr in pr_list:
            try:
                task_function(pr.repo, pr.pr_id)
                print(f"  [OK] {action_name.split('ing')[0]}ed {pr.repo}")
                wait_for_rollouts(pr.deployments)
            except (RuntimeError, urllib.error.URLError) as e:
                log_warn(
                    f"Failed to {action_name.lower()} PR for repo: {pr.repo}. Reason: {e}"
                )
    else:
        print(f"\n[INFO] {action_name} remaining PRs in parallel... This will be fast!")
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(task_function, pr.repo, pr.pr_id): pr for pr in pr_list
            }
            for future in concurrent.futures.as_completed(futures):
                pr = futures[future]
                try:
                    future.result()
                    print(f"  [OK] {action_name.split('ing')[0]}ed {pr.repo}")
                except (RuntimeError, urllib.error.URLError) as e:
                    log_warn(
                        f"Failed to {action_name.lower()} PR for repo: {pr.repo}. Reason: {e}"
                    )


def print_manual_review_queue(prs: List[PRData]):
    if not prs:
        return
    print(f"{CYAN}" + "=" * 60 + f"{NC}")
    print("MANUAL REVIEW REQUIRED")
    print(f"{CYAN}" + "=" * 60 + f"{NC}")
    for pr in prs:
        print(f"Repo: {pr.repo}")
        print(f"PR:   {pr.pr_url}")
        for reason in pr.review_reasons:
            print(f"  - {reason}")
        print()


def review_and_merge_prs(prs: List[PRData]):
    if not prs:
        return
    if not sys.stdin.isatty():
        log_warn("Review mode requires an interactive terminal.")
        return

    bulk_choice: Optional[str] = None
    for i, pr in enumerate(prs):
        if bulk_choice is not None:
            break
        os.system("cls" if os.name == "nt" else "clear")
        print(f"{CYAN}" + "=" * 60 + f"{NC}")
        print(f"PR Review: {pr.repo} ({i + 1}/{len(prs)})")
        print(pr.pr_url)
        print(f"{CYAN}" + "=" * 60 + f"{NC}")
        print("\n[DIFF]\n")
        run_cmd(
            [
                "git",
                "diff",
                "--color=always",
                f"origin/{pr.base_branch}...{pr.work_branch}",
            ],
            cwd=pr.repo_dir,
            capture=False,
        )
        print("\n" + "=" * 60)
        choice = read_single_key(
            "Action: [y] merge / [n] decline / [a] auto-merge safe remaining / [d] decline all remaining / [q] quit:",
            "ynadq",
        )

        if choice == "y":
            from resource_updater.config import WAIT_FOR_ROLLOUT
            from resource_updater.k8s import wait_for_rollouts

            print(f"\nMerging {pr.repo}...")
            try:
                merge_pull_request(pr.repo, pr.pr_id)
                print("  [OK] Merged successfully.")
                if WAIT_FOR_ROLLOUT:
                    wait_for_rollouts(pr.deployments)
            except (RuntimeError, urllib.error.URLError) as e:
                log_warn(f"Failed to merge PR for repo: {pr.repo}. Reason: {e}")
        elif choice == "n":
            print(f"\nDeclining {pr.repo}...")
            try:
                decline_pull_request(pr.repo, pr.pr_id)
                print("  [OK] Declined and removed successfully.")
            except (RuntimeError, urllib.error.URLError) as e:
                log_warn(f"Failed to decline PR for repo: {pr.repo}. Reason: {e}")
        elif choice == "a":
            remaining_prs = [pr] + prs[i + 1 :]
            safe_prs = [
                candidate for candidate in remaining_prs if candidate.auto_merge_safe
            ]
            flagged_prs = [
                candidate
                for candidate in remaining_prs
                if not candidate.auto_merge_safe
            ]
            if safe_prs:
                process_bulk_prs("Merging", safe_prs, merge_pull_request)
            else:
                print(
                    "\n[INFO] No remaining PRs qualify for auto-merge under the current thresholds."
                )
            if flagged_prs:
                print_manual_review_queue(flagged_prs)
                review_and_merge_prs(flagged_prs)
            break
        elif choice == "d":
            remaining_prs = [pr] + prs[i + 1 :]
            process_bulk_prs("Declining", remaining_prs, decline_pull_request)
            break
        elif choice == "q":
            print("\n[INFO] Exiting review mode. Remaining PRs left open.")
            break


def print_statistics(recs: List[Recommendation]):
    if not recs:
        return

    ns_stats = {}
    total_cur_cpu, total_tgt_cpu = 0, 0
    total_cur_mem, total_tgt_mem = 0, 0
    total_cur_limit_cpu, total_tgt_limit_cpu = 0, 0
    total_cur_limit_mem, total_tgt_limit_mem = 0, 0

    biggest_cpu_inc = {"name": None, "val": 0}
    biggest_cpu_dec = {"name": None, "val": 0}
    biggest_mem_inc = {"name": None, "val": 0}
    biggest_mem_dec = {"name": None, "val": 0}
    biggest_limit_cpu_inc = {"name": None, "val": 0}
    biggest_limit_cpu_dec = {"name": None, "val": 0}
    biggest_limit_mem_inc = {"name": None, "val": 0}
    biggest_limit_mem_dec = {"name": None, "val": 0}

    for rec in recs:
        c_cur = cpu_to_millis(rec.cpu_cur) if rec.cpu_cur != "not set" else 0
        c_tgt = cpu_to_millis(rec.cpu_tgt) if rec.cpu_tgt != "not set" else 0
        m_cur = memory_to_mi(rec.mem_cur) if rec.mem_cur != "not set" else 0
        m_tgt = memory_to_mi(rec.mem_tgt) if rec.mem_tgt != "not set" else 0
        lc_cur = (
            cpu_to_millis(rec.cpu_limit_cur) if rec.cpu_limit_cur != "not set" else 0
        )
        lc_tgt = (
            cpu_to_millis(rec.cpu_limit_tgt) if rec.cpu_limit_tgt != "not set" else 0
        )
        lm_cur = (
            memory_to_mi(rec.mem_limit_cur) if rec.mem_limit_cur != "not set" else 0
        )
        lm_tgt = (
            memory_to_mi(rec.mem_limit_tgt) if rec.mem_limit_tgt != "not set" else 0
        )

        c_cur = c_cur or 0
        c_tgt = c_tgt or 0
        m_cur = m_cur or 0
        m_tgt = m_tgt or 0
        lc_cur = lc_cur or 0
        lc_tgt = lc_tgt or 0
        lm_cur = lm_cur or 0
        lm_tgt = lm_tgt or 0

        cpu_diff = c_tgt - c_cur
        mem_diff = m_tgt - m_cur
        limit_cpu_diff = lc_tgt - lc_cur
        limit_mem_diff = lm_tgt - lm_cur

        if rec.namespace not in ns_stats:
            ns_stats[rec.namespace] = {
                "cur_cpu": 0,
                "tgt_cpu": 0,
                "cur_mem": 0,
                "tgt_mem": 0,
                "cur_limit_cpu": 0,
                "tgt_limit_cpu": 0,
                "cur_limit_mem": 0,
                "tgt_limit_mem": 0,
                "count": 0,
            }

        ns_stats[rec.namespace]["cur_cpu"] += c_cur
        ns_stats[rec.namespace]["tgt_cpu"] += c_tgt
        ns_stats[rec.namespace]["cur_mem"] += m_cur
        ns_stats[rec.namespace]["tgt_mem"] += m_tgt
        ns_stats[rec.namespace]["cur_limit_cpu"] += lc_cur
        ns_stats[rec.namespace]["tgt_limit_cpu"] += lc_tgt
        ns_stats[rec.namespace]["cur_limit_mem"] += lm_cur
        ns_stats[rec.namespace]["tgt_limit_mem"] += lm_tgt
        ns_stats[rec.namespace]["count"] += 1

        total_cur_cpu += c_cur
        total_tgt_cpu += c_tgt
        total_cur_mem += m_cur
        total_tgt_mem += m_tgt
        total_cur_limit_cpu += lc_cur
        total_tgt_limit_cpu += lc_tgt
        total_cur_limit_mem += lm_cur
        total_tgt_limit_mem += lm_tgt

        deploy_label = f"{rec.namespace}/{rec.deployment.replace('-deployment', '')}"

        if cpu_diff > biggest_cpu_inc["val"]:
            biggest_cpu_inc = {"name": deploy_label, "val": cpu_diff}
        if cpu_diff < biggest_cpu_dec["val"]:
            biggest_cpu_dec = {"name": deploy_label, "val": cpu_diff}
        if mem_diff > biggest_mem_inc["val"]:
            biggest_mem_inc = {"name": deploy_label, "val": mem_diff}
        if mem_diff < biggest_mem_dec["val"]:
            biggest_mem_dec = {"name": deploy_label, "val": mem_diff}
        if limit_cpu_diff > biggest_limit_cpu_inc["val"]:
            biggest_limit_cpu_inc = {"name": deploy_label, "val": limit_cpu_diff}
        if limit_cpu_diff < biggest_limit_cpu_dec["val"]:
            biggest_limit_cpu_dec = {"name": deploy_label, "val": limit_cpu_diff}
        if limit_mem_diff > biggest_limit_mem_inc["val"]:
            biggest_limit_mem_inc = {"name": deploy_label, "val": limit_mem_diff}
        if limit_mem_diff < biggest_limit_mem_dec["val"]:
            biggest_limit_mem_dec = {"name": deploy_label, "val": limit_mem_diff}

    def color_diff(diff_str: str) -> str:
        if "(+" in diff_str:
            return f"{RED}{diff_str}{NC}"
        elif "(-" in diff_str:
            return f"{GREEN}{diff_str}{NC}"
        return diff_str

    print(f"\n{BOLD}{CYAN}=== CLUSTER-WIDE SUMMARY ==={NC}")
    summary_headers = ["Resource", "Current", "Target", "Difference"]
    summary_rows = [
        [
            "CPU Requests",
            format_val(total_cur_cpu, "m"),
            format_val(total_tgt_cpu, "m"),
            color_diff(
                format_diff_detailed(total_cur_cpu, total_tgt_cpu, "m").split(" ", 3)[
                    -1
                ]
            ),
        ],
        [
            "Mem Requests",
            format_val(total_cur_mem, "Mi"),
            format_val(total_tgt_mem, "Mi"),
            color_diff(
                format_diff_detailed(total_cur_mem, total_tgt_mem, "Mi").split(" ", 3)[
                    -1
                ]
            ),
        ],
        [
            "CPU Limits",
            format_val(total_cur_limit_cpu, "m"),
            format_val(total_tgt_limit_cpu, "m"),
            color_diff(
                format_diff_detailed(
                    total_cur_limit_cpu, total_tgt_limit_cpu, "m"
                ).split(" ", 3)[-1]
            ),
        ],
        [
            "Mem Limits",
            format_val(total_cur_limit_mem, "Mi"),
            format_val(total_tgt_limit_mem, "Mi"),
            color_diff(
                format_diff_detailed(
                    total_cur_limit_mem, total_tgt_limit_mem, "Mi"
                ).split(" ", 3)[-1]
            ),
        ],
    ]
    print_table(summary_headers, summary_rows)

    print(f"\n{BOLD}{CYAN}=== NAMESPACE SUMMARY ==={NC}")
    ns_headers = [
        "Namespace",
        "Deployments",
        "CPU Req Diff",
        "Mem Req Diff",
        "CPU Lim Diff",
        "Mem Lim Diff",
    ]
    ns_rows = []
    for ns in sorted(ns_stats.keys()):
        stats = ns_stats[ns]
        ns_rows.append(
            [
                ns,
                str(stats["count"]),
                color_diff(
                    format_diff_detailed(stats["cur_cpu"], stats["tgt_cpu"], "m").split(
                        " ", 3
                    )[-1]
                ),
                color_diff(
                    format_diff_detailed(
                        stats["cur_mem"], stats["tgt_mem"], "Mi"
                    ).split(" ", 3)[-1]
                ),
                color_diff(
                    format_diff_detailed(
                        stats["cur_limit_cpu"], stats["tgt_limit_cpu"], "m"
                    ).split(" ", 3)[-1]
                ),
                color_diff(
                    format_diff_detailed(
                        stats["cur_limit_mem"], stats["tgt_limit_mem"], "Mi"
                    ).split(" ", 3)[-1]
                ),
            ]
        )
    print_table(ns_headers, ns_rows)

    print(f"\n{BOLD}{CYAN}=== NOTABLE DEPLOYMENTS ==={NC}")
    if biggest_cpu_inc["name"]:
        print(
            f"  {RED}↑{NC} Highest Request CPU Increase:  {biggest_cpu_inc['name']} (+{format_val(biggest_cpu_inc['val'], 'm')})"
        )
    if biggest_cpu_dec["name"]:
        print(
            f"  {GREEN}↓{NC} Highest Request CPU Savings:   {biggest_cpu_dec['name']} ({format_val(biggest_cpu_dec['val'], 'm')})"
        )
    if biggest_mem_inc["name"]:
        print(
            f"  {RED}↑{NC} Highest Request Mem Increase:  {biggest_mem_inc['name']} (+{format_val(biggest_mem_inc['val'], 'Mi')})"
        )
    if biggest_mem_dec["name"]:
        print(
            f"  {GREEN}↓{NC} Highest Request Mem Savings:   {biggest_mem_dec['name']} ({format_val(biggest_mem_dec['val'], 'Mi')})"
        )
    if biggest_limit_cpu_inc["name"]:
        print(
            f"  {RED}↑{NC} Highest Limit CPU Increase:    {biggest_limit_cpu_inc['name']} (+{format_val(biggest_limit_cpu_inc['val'], 'm')})"
        )
    if biggest_limit_cpu_dec["name"]:
        print(
            f"  {GREEN}↓{NC} Highest Limit CPU Savings:     {biggest_limit_cpu_dec['name']} ({format_val(biggest_limit_cpu_dec['val'], 'm')})"
        )
    if biggest_limit_mem_inc["name"]:
        print(
            f"  {RED}↑{NC} Highest Limit Mem Increase:    {biggest_limit_mem_inc['name']} (+{format_val(biggest_limit_mem_inc['val'], 'Mi')})"
        )
    if biggest_limit_mem_dec["name"]:
        print(
            f"  {GREEN}↓{NC} Highest Limit Mem Savings:     {biggest_limit_mem_dec['name']} ({format_val(biggest_limit_mem_dec['val'], 'Mi')})"
        )
    print()
