import shutil
from pathlib import Path
from typing import Optional, Tuple, List

from resource_updater.utils import run_cmd
from resource_updater.config import (
    REPOS_DIR,
    KUSTOMIZE_REPOS_DIR,
    KUSTOMIZE_MONOREPO_NAME,
    BRANCH,
    UPDATE_COMMIT_MSG,
    REVERT_COMMIT_MSG,
)
from resource_updater.bitbucket import repo_remote_url


def get_repo(reponame: str, work_branch: str) -> Tuple[Path, str]:
    dir_path = REPOS_DIR / reponame
    if reponame == KUSTOMIZE_MONOREPO_NAME and (
        (KUSTOMIZE_REPOS_DIR / "environments").exists()
        or (KUSTOMIZE_REPOS_DIR / ".git").exists()
    ):
        local_repo = KUSTOMIZE_REPOS_DIR
    else:
        local_repo = KUSTOMIZE_REPOS_DIR / reponame
    remote_url = repo_remote_url(reponame)

    if (dir_path / ".git").exists():
        run_cmd(["git", "remote", "set-url", "origin", remote_url], cwd=dir_path)
    elif (local_repo / ".git").exists():
        shutil.rmtree(dir_path, ignore_errors=True)
        run_cmd(["git", "clone", "--quiet", str(local_repo), str(dir_path)])
        run_cmd(["git", "remote", "set-url", "origin", remote_url], cwd=dir_path)
    elif local_repo.exists():
        shutil.rmtree(dir_path, ignore_errors=True)
        shutil.copytree(local_repo, dir_path, dirs_exist_ok=True)
        run_cmd(["git", "init", "-q"], cwd=dir_path)
        run_cmd(["git", "add", "-A"], cwd=dir_path)
        run_cmd(["git", "commit", "-m", "Initial commit"], cwd=dir_path)
        run_cmd(["git", "remote", "add", "origin", remote_url], cwd=dir_path)
    else:
        shutil.rmtree(dir_path, ignore_errors=True)
        run_cmd(["git", "clone", "--depth", "1", remote_url, str(dir_path)])

    if run_cmd(["git", "diff", "--quiet"], cwd=dir_path).returncode != 0:
        run_cmd(["git", "stash", "push", "-u", "-m", "resource-update"], cwd=dir_path)

    run_cmd(["git", "fetch", "origin", "--prune"], cwd=dir_path)

    base_branch = "master"
    if (
        run_cmd(
            ["git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"],
            cwd=dir_path,
        ).returncode
        == 0
    ):
        base_branch = "main"

    run_cmd(
        ["git", "checkout", "-B", base_branch, f"origin/{base_branch}"], cwd=dir_path
    )
    run_cmd(
        ["git", "push", "origin", "--delete", work_branch], cwd=dir_path, check=False
    )
    run_cmd(["git", "branch", "-D", work_branch], cwd=dir_path, check=False)
    run_cmd(["git", "checkout", "-B", work_branch, base_branch], cwd=dir_path)

    return dir_path, base_branch


def commit_push(dir_path: Path, work_branch: str, commit_msg: str) -> bool:
    if (
        run_cmd(["git", "diff", "--quiet"], cwd=dir_path).returncode == 0
        and run_cmd(["git", "diff", "--cached", "--quiet"], cwd=dir_path).returncode
        == 0
    ):
        return False

    run_cmd(["git", "add", "-A"], cwd=dir_path, check=True)
    run_cmd(["git", "commit", "-m", commit_msg], cwd=dir_path, check=True)

    local_head = run_cmd(["git", "rev-parse", "HEAD"], cwd=dir_path).stdout.strip()
    remote_head = run_cmd(
        ["git", "rev-parse", f"origin/{work_branch}"], cwd=dir_path
    ).stdout.strip()

    if local_head == remote_head:
        return False

    res = run_cmd(["git", "push", "-u", "origin", work_branch], cwd=dir_path, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Git push failed: {res.stderr.strip()}")
    return True


def is_resource_update_history_message(msg: str, env: str) -> bool:
    env_lower = env.lower()

    if UPDATE_COMMIT_MSG in msg or REVERT_COMMIT_MSG in msg:
        return True

    branch_markers = {
        BRANCH,
        f"{BRANCH}-{env_lower}",
        f"{BRANCH}-revert-{env_lower}",
    }
    merge_markers = ("Merged in", "pull request")

    if any(marker in msg for marker in branch_markers):
        return True

    if any(marker in msg for marker in merge_markers) and BRANCH in msg:
        return True

    return False


def get_history_ref(repo_dir: Path, base_branch: str) -> str:
    remote_ref = f"refs/remotes/origin/{base_branch}"
    if (
        run_cmd(
            ["git", "show-ref", "--verify", "--quiet", remote_ref], cwd=repo_dir
        ).returncode
        == 0
    ):
        return f"origin/{base_branch}"
    return base_branch


def describe_commit(repo_dir: Path, rev: str) -> str:
    res = run_cmd(
        ["git", "show", "-s", "--format=%h %s", rev], cwd=repo_dir, check=False
    )
    if res.returncode == 0:
        return res.stdout.strip()
    return rev


def find_resource_update_restore_source(
    repo_dir: Path,
    base_branch: str,
    relative_path: str,
    env: str,
) -> Tuple[str, Optional[str], Optional[str], int]:
    history_ref = get_history_ref(repo_dir, base_branch)
    res = run_cmd(
        ["git", "log", "--follow", "--format=%H|%s", history_ref, "--", relative_path],
        cwd=repo_dir,
    )
    lines = [line for line in res.stdout.strip().splitlines() if line]

    update_hashes: List[str] = []
    restore_source: Optional[str] = None
    restore_display: Optional[str] = None

    for line in lines:
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue

        commit_hash, msg = parts
        if is_resource_update_history_message(msg, env):
            update_hashes.append(commit_hash)
            continue

        if update_hashes:
            restore_source = commit_hash
            restore_display = commit_hash[:7]
        break

    if not update_hashes:
        return history_ref, None, None, 0

    if restore_source is not None:
        return history_ref, restore_source, restore_display, len(update_hashes)

    earliest_update = update_hashes[-1]
    parent_res = run_cmd(
        ["git", "rev-parse", f"{earliest_update}^"], cwd=repo_dir, check=False
    )
    if parent_res.returncode == 0:
        restore_display = parent_res.stdout.strip()[:7]
        return (
            history_ref,
            f"{earliest_update}^",
            restore_display,
            len(update_hashes),
        )

    return history_ref, None, None, len(update_hashes)

