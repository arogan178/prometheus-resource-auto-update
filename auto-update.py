#!/usr/bin/env python3

"""
Prometheus Resource Auto-Update Script
Fetches Prometheus usage recommendations, updates service repos, and creates PRs
natively via the Bitbucket API. Includes Revert Mode for previous automated updates.
"""

import getpass
import concurrent.futures
import os
import re
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# Important: Load config/env first
from resource_updater.config import (
    TODAY,
    REPOS_DIR,
    PROMETHEUS_DAYS,
    PROMETHEUS_PERCENTILE,
    MAX_WORKERS_REPOS,
)
from resource_updater.utils import (
    log,
    log_warn,
    read_single_key,
    Spinner,
    log_success,
    CYAN,
    NC,
)
from resource_updater.models import Recommendation, PRData, SkippedRecommendation
from resource_updater.bitbucket import (
    get_bitbucket_auth,
    validate_bitbucket_credentials,
    _bitbucket_auth_requirements,
)
from resource_updater.k8s import get_cluster, get_prom_recommendations, get_namespaces
from resource_updater.kustomize import (
    build_ns_repo_map,
    resolve_repo_for_deployment,
    is_prefix_key,
)
from resource_updater.processor import (
    process_single_repo,
    process_revert_single_repo,
    review_and_merge_prs,
    print_statistics,
    generate_markdown_report,
)
from resource_updater.html_report import (
    generate_html_report,
    generate_report_history_index,
    generate_report_index,
)
from resource_updater.resources import (
    get_auto_merge_thresholds,
    get_request_tightening_policy,
    get_bulk_retain_policy,
    get_idle_workload_policy,
    format_val,
    format_memory_mi,
)

print_lock = threading.Lock()
VALID_DRY_RUN_ENVIRONMENTS = ("dev", "stg", "uat", "prd")
CPE_DRY_RUN_ENVIRONMENTS = ("uat", "stg", "prd")


@dataclass(frozen=True)
class EnvironmentTarget:
    env: str
    target_namespaces: list[str]


@dataclass(frozen=True)
class StartupConfig:
    environment_targets: list[EnvironmentTarget]
    operation_mode: str
    pr_action: str
    multi_env_extraction: bool


@dataclass(frozen=True)
class UpdateResult:
    env: str
    updated: int
    failed: int
    applied_count: int
    skipped_count: int = 0
    output_file: Optional[Path] = None
    html_output_file: Optional[Path] = None


def ensure_bitbucket_credentials():
    auth = get_bitbucket_auth()
    if auth is not None:
        validate_bitbucket_credentials()
        return

    print("\n[AUTH] Bitbucket Authentication Required")
    print("This script uses the official Bitbucket REST API to manage Pull Requests.")
    print("Use one of these combinations:")
    print("  • BITBUCKET_EMAIL + BITBUCKET_API_TOKEN")
    print("  • BITBUCKET_USERNAME + BITBUCKET_APP_PASSWORD")
    print("Generate tokens here: https://bitbucket.org/account/settings/\n")

    email = input(
        "Enter your Atlassian Email (leave blank to use username/app password): "
    ).strip()
    if email:
        os.environ["BITBUCKET_EMAIL"] = email
        api_token = getpass.getpass("Enter your Bitbucket API Token: ").strip()
        os.environ["BITBUCKET_API_TOKEN"] = api_token
    else:
        username = input("Enter your Bitbucket Username: ").strip()
        os.environ["BITBUCKET_USERNAME"] = username
        app_password = getpass.getpass("Enter your Bitbucket App Password: ").strip()
        os.environ["BITBUCKET_APP_PASSWORD"] = app_password

    auth = get_bitbucket_auth()
    if auth is None:
        sys.exit(
            f"\n[ERROR] Missing Bitbucket credentials. {_bitbucket_auth_requirements()}"
        )

    validate_bitbucket_credentials()
    print("[OK] Credentials saved for this session.\n")


def filter_namespaces(cluster_namespaces: list[str]) -> list[str]:
    namespace_include_regex = os.getenv("NAMESPACE_INCLUDE_REGEX", "").strip()
    if not namespace_include_regex:
        return sorted(cluster_namespaces)

    try:
        namespace_pattern = re.compile(namespace_include_regex)
    except re.error as exc:
        sys.exit(f"\n[ERROR] Invalid NAMESPACE_INCLUDE_REGEX: {exc}")

    return sorted(ns for ns in cluster_namespaces if namespace_pattern.search(ns))


def namespaces_for_environment(cluster_namespaces: list[str], env: str) -> list[str]:
    env_suffix = f"-{env}01"
    return sorted(ns for ns in cluster_namespaces if ns.endswith(env_suffix))


def infer_dry_run_environments(cluster_namespaces: list[str]) -> list[str]:
    if namespaces_for_environment(cluster_namespaces, "dev"):
        return ["dev"]

    return [
        env
        for env in CPE_DRY_RUN_ENVIRONMENTS
        if namespaces_for_environment(cluster_namespaces, env)
    ]


def build_environment_targets(
    cluster_namespaces: list[str], environments: list[str]
) -> list[EnvironmentTarget]:
    return [
        EnvironmentTarget(env=env, target_namespaces=namespaces)
        for env in environments
        if (namespaces := namespaces_for_environment(cluster_namespaces, env))
    ]


def create_dry_run_report_dir() -> Path:
    output_dir = Path("resource_update_reports") / datetime.now().strftime(
        "%Y-%m-%d_%H%M%S"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def startup_sequence() -> StartupConfig:
    os.system("cls" if os.name == "nt" else "clear")
    print(f"{CYAN}" + "=" * 60 + f"{NC}")
    print("Prometheus Resource Auto-Update")
    print(f"{CYAN}" + "=" * 60 + f"{NC}")

    ensure_bitbucket_credentials()

    print("\n[WARNING] REMINDER: You must be authenticated to the correct cluster.")
    print(
        "          Please ensure you have run 'oc login' or 'kubectl config use-context' before proceeding.\n"
    )

    ready = read_single_key("Are you logged in to the right cluster? [y/N]:", "yn")
    if ready == "n":
        sys.exit(
            "\nPlease run 'oc login' or 'kubectl config use-context' for the target environment and try again."
        )

    print()
    with Spinner("Fetching namespaces from the cluster..."):
        cluster_namespaces = get_namespaces()

    if not cluster_namespaces:
        sys.exit(
            "\n[ERROR] Could not fetch namespaces from cluster. Are you sure you are logged in via 'oc login' or 'kubectl'?"
        )

    all_namespaces = filter_namespaces(cluster_namespaces)

    if not all_namespaces:
        namespace_include_regex = os.getenv("NAMESPACE_INCLUDE_REGEX", "").strip()
        sys.exit(
            "\n[ERROR] No namespaces found in the current cluster"
            + (
                f" matching NAMESPACE_INCLUDE_REGEX={namespace_include_regex!r}."
                if namespace_include_regex
                else "."
            )
        )

    print("\nWould you like to run multi-environment dry-run extraction?")
    print("  [y] Yes - infer environments from namespace suffixes and write report files only")
    print("      -dev01 namespaces -> DEV; -uat01/-stg01/-prd01 namespaces -> UAT/STG/PRD")
    print("  [n] No - use the standard single-environment flow")
    multi_env_choice = read_single_key("\nChoice [y/n]:", "yn")
    if multi_env_choice == "y":
        environments = infer_dry_run_environments(all_namespaces)
        environment_targets = build_environment_targets(all_namespaces, environments)
        if not environment_targets:
            sys.exit(
                "\n[ERROR] No namespaces ending in -dev01, -uat01, -stg01, or -prd01 were found."
            )

        print("\nMulti-environment dry-run extraction targets:")
        for target in environment_targets:
            print(
                f"  {target.env.upper()}: {len(target.target_namespaces)} namespace(s)"
            )

        return StartupConfig(
            environment_targets=environment_targets,
            operation_mode="update",
            pr_action="f",
            multi_env_extraction=True,
        )

    print()
    while True:
        env = input("Which environment or cluster alias would you like to process?: ").strip()
        if env:
            break
        print("Please enter a non-empty environment or cluster alias.")

    print(f"\nNamespaces available for '{env}':")
    for i, ns in enumerate(all_namespaces):
        print(f"  {i + 1}. {ns}")

    print("\nSelect an option:")
    print("  [Enter] Process ALL namespaces")
    print(f"  [1-{len(all_namespaces)}]  Process a specific namespace by number")

    while True:
        ns_choice = input("\nChoice: ").strip()
        if not ns_choice:
            target_namespaces = all_namespaces
            break
        if ns_choice.isdigit() and 1 <= int(ns_choice) <= len(all_namespaces):
            target_namespaces = [all_namespaces[int(ns_choice) - 1]]
            break
        print(
            "Invalid choice. Leave it blank for all namespaces, or type a number from the list."
        )

    print("\nWhat action would you like to perform?")
    print("  [u] Update resources based on current Prometheus recommendations")
    print(
        "  [v] Revert the last merged resource update (restore limits to previous state)"
    )
    operation_mode = read_single_key("\nChoice [u/v]:", "uv")
    operation_mode = "update" if operation_mode == "u" else "revert"

    print("\nHow would you like to handle the Pull Requests?")
    print(
        "  [r] Review them interactively (press 'a' during review to auto-merge safe remaining PRs)"
    )
    print("  [l] Leave them open (do nothing)")
    if operation_mode == "update":
        print("  [f] File output only (Dry run: no file changes, no commits, no PRs)")
        pr_action = read_single_key("\nChoice [r/l/f]:", "rlf")
    else:
        print("  [f] Dry run only (Logs repos that would be reverted, no PRs)")
        pr_action = read_single_key("\nChoice [r/l/f]:", "rlf")

    return StartupConfig(
        environment_targets=[
            EnvironmentTarget(env=env, target_namespaces=target_namespaces)
        ],
        operation_mode=operation_mode,
        pr_action=pr_action,
        multi_env_extraction=False,
    )


def execute_update(
    env: str,
    target_namespaces: list[str],
    pr_action: str,
    report_dir: Optional[Path] = None,
) -> UpdateResult:
    with Spinner("Fetching Prometheus usage data..."):
        all_recs = []
        for ns in target_namespaces:
            all_recs.extend(get_prom_recommendations(ns))

    log_success(f"Found {len(all_recs)} deployments with changes")
    print()
    if not all_recs:
        print("Nothing to update.")

    log("Building namespace to repo mapping...")
    ns_repo_map = build_ns_repo_map(env, target_namespaces)
    cpu_threshold, mem_threshold = get_auto_merge_thresholds()
    cpu_factor, mem_factor = get_request_tightening_policy()
    request_retain_factor, limit_retain_factor = get_bulk_retain_policy()
    (
        idle_cpu_threshold_millis,
        idle_memory_threshold_mi,
        idle_request_cpu_millis,
        idle_request_memory_mi,
        idle_limit_cpu_millis,
        idle_limit_memory_mi,
    ) = get_idle_workload_policy()
    log(
        f"Auto-merge thresholds: CPU <= {format_val(cpu_threshold, 'm')}, Memory <= {format_memory_mi(mem_threshold)}"
    )
    log(
        f"Request tightening policy: CPU target x {cpu_factor:.2f}, Memory target x {mem_factor:.2f}"
    )
    log(
        f"Bulk downscale safety: keep >= {request_retain_factor:.0%} of current requests and >= {limit_retain_factor:.0%} of current limits per run"
    )
    log(
        "Idle workload floors: "
        f"if P95 <= {format_val(idle_cpu_threshold_millis, 'm')} CPU and {format_memory_mi(idle_memory_threshold_mi)} memory, "
        f"requests >= {format_val(idle_request_cpu_millis, 'm')} / {format_memory_mi(idle_request_memory_mi)}, "
        f"limits >= {format_val(idle_limit_cpu_millis, 'm')} / {format_memory_mi(idle_limit_memory_mi)}"
    )

    updated, failed = 0, 0
    applied_recs: list[Recommendation] = []
    skipped_recs: list[SkippedRecommendation] = []
    created_prs: list[PRData] = []
    created_pr_repos = set()
    pr_links = set()
    repo_recommendations: dict[str, list[Recommendation]] = defaultdict(list)

    for rec in all_recs:
        ns_mapping = ns_repo_map.get(rec.namespace, {})
        repo = resolve_repo_for_deployment(ns_mapping, rec.deployment)
        if not repo:
            reason = (
                f"Could not find repo for namespace: {rec.namespace}, deployment: {rec.deployment}"
            )
            log_warn(f"{reason}\n")
            skipped_recs.append(
                SkippedRecommendation(
                    namespace=rec.namespace,
                    deployment=rec.deployment,
                    repo="unmapped",
                    category="Repo mapping",
                    reason=reason,
                )
            )
            continue
        repo_recommendations[repo].append(rec)

    log("Processing repositories in parallel... This will be fast!\n")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS_REPOS) as executor:
        futures = {
            executor.submit(
                process_single_repo, repo, repo_recs, env, pr_action
            ): repo
            for repo, repo_recs in repo_recommendations.items()
        }
        for future in concurrent.futures.as_completed(futures):
            logs, u_count, f_count, pr_data, repo_applied_recs = future.result()
            with print_lock:
                for log_line in logs:
                    print(log_line)
                print()
            updated += u_count
            failed += f_count
            applied_recs.extend(repo_applied_recs)
            if pr_data and pr_data.repo not in created_pr_repos:
                created_prs.append(pr_data)
                created_pr_repos.add(pr_data.repo)
                pr_links.add(pr_data.pr_url)

    if created_prs and pr_action == "r":
        review_and_merge_prs(created_prs)

    print(f"{CYAN}" + "=" * 60 + f"{NC}")
    if pr_action == "f":
        log(
            f"Summary: Patchable: {len(applied_recs)}, "
            f"Skipped/Unpatchable: {len(skipped_recs)}, Failed: {failed}"
        )
    else:
        log(f"Summary: Updated: {updated}, Failed: {failed}")
    print()
    print_statistics(applied_recs)

    output_file = None
    html_file = None
    if pr_action == "f":
        report_filename = (
            f"resource_changes_{env}.md"
            if report_dir
            else f"resource_changes_{env}_{TODAY}.md"
        )
        output_file = (
            report_dir / report_filename if report_dir else Path(report_filename)
        )
        title = f"Prometheus Resource Changes - Environment: {env.upper()}"
        generate_markdown_report(
            output_file,
            env,
            title,
            applied_recs,
            PROMETHEUS_DAYS,
            PROMETHEUS_PERCENTILE,
            skipped_recs,
        )
        html_file = output_file.with_suffix(".html")
        generate_html_report(
            html_file,
            env,
            title,
            applied_recs,
            PROMETHEUS_DAYS,
            PROMETHEUS_PERCENTILE,
            skipped_recs,
            report_home_href="../index.html" if report_dir else None,
            run_index_href="index.html" if report_dir else None,
        )

        print(f"{CYAN}" + "=" * 60 + f"{NC}")
        log(f"Dry run complete. Detailed changes saved to: {output_file.absolute()}")
        log(f"Visual dashboard: {html_file.absolute()}")
        print("=" * 60 + "\n")

    if pr_links:
        print("=" * 60 + "\nOPEN PR PAGES\n" + "=" * 60)
        for link in sorted(pr_links):
            print(link)

    return UpdateResult(
        env=env,
        updated=updated,
        failed=failed,
        applied_count=len(applied_recs),
        skipped_count=len(skipped_recs),
        output_file=output_file,
        html_output_file=html_file,
    )


def execute_revert(env: str, target_namespaces: list[str], pr_action: str):
    log("Building namespace to repo mapping for target environments...")
    ns_repo_map = build_ns_repo_map(env, target_namespaces)

    repo_to_ns_deploy: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ns, mapping in ns_repo_map.items():
        for deploy, repo in mapping.items():
            if is_prefix_key(deploy):
                continue
            repo_to_ns_deploy[repo].append(
                (ns, "*") if deploy == "*" else (ns, deploy)
            )

    if not repo_to_ns_deploy:
        sys.exit(
            "No mapped repositories found for the selected namespaces. Exiting."
        )

    log(
        f"Found {len(repo_to_ns_deploy)} repositories to inspect for resource updates."
    )
    log("Processing repositories in parallel... This will be fast!\n")

    updated, failed = 0, 0
    created_prs: list[PRData] = []
    applied_recs: list[Recommendation] = []
    pr_links = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS_REPOS) as executor:
        futures = {
            executor.submit(
                process_revert_single_repo, repo, ns_deploy_list, env, pr_action
            ): repo
            for repo, ns_deploy_list in repo_to_ns_deploy.items()
        }
        for future in concurrent.futures.as_completed(futures):
            logs, u_count, f_count, pr_data, repo_applied_recs = future.result()
            with print_lock:
                for log_line in logs:
                    print(log_line)
                print()
            updated += u_count
            failed += f_count
            applied_recs.extend(repo_applied_recs)
            if pr_data:
                created_prs.append(pr_data)
                pr_links.add(pr_data.pr_url)

    if created_prs and pr_action == "r":
        review_and_merge_prs(created_prs)

    print(f"{CYAN}" + "=" * 60 + f"{NC}")
    log(f"Summary: Reverted: {updated}, Failed/Skipped: {failed}\n")
    print_statistics(applied_recs)

    if pr_action == "f":
        output_file = Path(
            f"resource_reverts_{env}_{TODAY}.md"
        )
        title = f"Resource Revert Candidates - Environment: {env.upper()}"
        generate_markdown_report(
            output_file,
            env,
            title,
            applied_recs,
            PROMETHEUS_DAYS,
            PROMETHEUS_PERCENTILE,
        )

        print(f"{CYAN}" + "=" * 60 + f"{NC}")
        log(f"Dry run complete. File saved to: {output_file.absolute()}")
        print("=" * 60 + "\n")

    if pr_links:
        print("=" * 60 + "\nOPEN PR PAGES (REVERTS)\n" + "=" * 60)
        for link in sorted(pr_links):
            print(link)


def main():
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    config = startup_sequence()

    environment_targets = config.environment_targets
    total_namespaces = sum(len(target.target_namespaces) for target in environment_targets)
    target_envs = ", ".join(target.env.upper() for target in environment_targets)
    target_clusters = ", ".join(
        sorted({get_cluster(target.env).upper() for target in environment_targets})
    )
    report_dir = (
        create_dry_run_report_dir()
        if config.multi_env_extraction and config.pr_action == "f"
        else None
    )

    os.system("cls" if os.name == "nt" else "clear")
    print(f"{CYAN}" + "=" * 60 + f"{NC}")
    log(f"Targeting Environment(s): {target_envs} (Cluster: {target_clusters})")
    log(f"Targeting Namespaces:    {total_namespaces} selected")
    if config.pr_action == "f":
        log("Execution Mode: DRY RUN (File Output Only)")
    if report_dir:
        log(f"Report Directory: {report_dir.absolute()}")
    print("=" * 60 + "\n")

    if config.operation_mode == "update":
        results = []
        for target in environment_targets:
            if len(environment_targets) > 1:
                print(f"{CYAN}" + "=" * 60 + f"{NC}")
                log(
                    f"Processing {target.env.upper()} ({len(target.target_namespaces)} namespace(s))"
                )
                print("=" * 60 + "\n")
            results.append(
                execute_update(
                    target.env,
                    target.target_namespaces,
                    config.pr_action,
                    report_dir=report_dir,
                )
            )

        run_index = None
        history_index = None
        if report_dir and config.pr_action == "f":
            run_index = generate_report_index(
                report_dir,
                results,
                "Dry-run report run",
                "",
                target_envs,
            )
            history_index = generate_report_history_index(report_dir.parent)

        if len(results) > 1:
            print(f"{CYAN}" + "=" * 60 + f"{NC}")
            log("Multi-environment dry-run extraction complete.")
            for result in results:
                output = (
                    f" -> {result.output_file.absolute()}" if result.output_file else ""
                )
                log(
                    f"{result.env.upper()}: Patchable: {result.applied_count}, "
                    f"Skipped/Unpatchable: {result.skipped_count}, "
                    f"Failed: {result.failed}{output}"
                )
            if run_index:
                log(f"Environments in this run: {run_index.absolute()}")
            if history_index:
                log(f"All report runs (share this): {history_index.absolute()}")
            print("=" * 60 + "\n")
        elif run_index:
            print(f"{CYAN}" + "=" * 60 + f"{NC}")
            log(f"Environments in this run: {run_index.absolute()}")
            if history_index:
                log(f"All report runs (share this): {history_index.absolute()}")
            print("=" * 60 + "\n")
    elif config.operation_mode == "revert":
        target = environment_targets[0]
        execute_revert(target.env, target.target_namespaces, config.pr_action)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nScript aborted by user.")
        sys.exit(1)
