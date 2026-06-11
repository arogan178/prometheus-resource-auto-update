import os
import re
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Set

from resource_updater.config import (
    REPOS_DIR,
    CLUSTER_CONFIG,
    KUSTOMIZE_REPOS_DIR,
    KUSTOMIZE_MONOREPO_NAME,
)
from resource_updater.k8s import get_cluster
from resource_updater.utils import run_cmd

PREFIX_KEY = "__prefix__:"
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "CronJob", "Job"}
REPO_PREFIXES = tuple(
    prefix
    for prefix in os.getenv("REPO_NAME_PREFIXES", os.getenv("REPO_PREFIX", "")).split(",")
    if prefix
)
REPO_ALIAS_PREFIXES = tuple(
    prefix for prefix in os.getenv("REPO_ALIAS_PREFIXES", "").split(",") if prefix
)
ENV_RENDER_CACHE: Dict[Path, Optional[str]] = {}
ENV_MATCHER_CACHE: Dict[Path, Tuple[Set[str], Set[str]]] = {}
REPO_ENV_DIR_CACHE: Dict[Tuple[str, str, str], List[Path]] = {}
KUSTOMIZATION_NAMESPACE_CACHE: Dict[Path, Optional[str]] = {}
CLUSTER_NAMESPACE_REPO_ENV_CACHE: Dict[str, Dict[str, Dict[str, List[Path]]]] = {}


def is_prefix_key(key: str) -> bool:
    return key.startswith(PREFIX_KEY)


def _prefix_key(prefix: str) -> str:
    return f"{PREFIX_KEY}{prefix}"


def _extract_prefix(key: str) -> str:
    return key[len(PREFIX_KEY) :]


def _strip_repo_prefix(repo_name: str) -> str:
    for prefix in REPO_PREFIXES:
        if repo_name.startswith(prefix):
            return repo_name[len(prefix) :]
    return repo_name


def _repo_aliases(repo_name: str) -> Set[str]:
    aliases = {repo_name, repo_name.replace("-devops-", "-")}
    for alias in list(aliases):
        trimmed = alias
        changed = True
        while changed:
            changed = False
            for prefix in REPO_ALIAS_PREFIXES:
                if trimmed.startswith(prefix):
                    trimmed = trimmed[len(prefix) :]
                    changed = True
        if trimmed:
            aliases.add(trimmed)
    return {alias for alias in aliases if alias}


def _register_mapping(
    mapping: Dict[str, str], key: str, repo_name: str, prefer: bool = False
) -> None:
    if not key:
        return
    if prefer or key not in mapping:
        mapping[key] = repo_name


def _register_repo_aliases(
    mapping: Dict[str, str], repo_name: str, prefer: bool = False
) -> None:
    for alias in _repo_aliases(repo_name):
        _register_mapping(mapping, alias, repo_name, prefer=prefer)
        _register_mapping(mapping, _prefix_key(alias), repo_name, prefer=prefer)


def _read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text()


def _repo_names_from_apps_file(path: Path) -> List[str]:
    content = _read_text_if_exists(path)
    if not content:
        return []
    return [_strip_repo_prefix(name) for name in re.findall(r"([^/]+)\.git", content)]


def _looks_like_kustomize_repo(path: Path) -> bool:
    return (path / "environments").is_dir() or (path / "kustomization.yaml").exists()


def _local_kustomize_repos() -> List[Tuple[str, Path]]:
    if not KUSTOMIZE_REPOS_DIR.exists():
        return []

    if _is_monorepo_layout():
        return [(KUSTOMIZE_MONOREPO_NAME, KUSTOMIZE_REPOS_DIR)]

    repos = []
    for repo_dir in KUSTOMIZE_REPOS_DIR.iterdir():
        if repo_dir.is_dir() and _looks_like_kustomize_repo(repo_dir):
            repos.append((repo_dir.name, repo_dir))
    return sorted(repos)


def _is_monorepo_layout() -> bool:
    return KUSTOMIZE_REPOS_DIR.exists() and _looks_like_kustomize_repo(
        KUSTOMIZE_REPOS_DIR
    )


def _kustomization_matches_namespace(kustomization: Path, namespace: str) -> bool:
    detected_namespace = _namespace_from_kustomization(kustomization)
    if detected_namespace:
        return detected_namespace == namespace
    content = _read_text_if_exists(kustomization)
    if not content:
        return False
    return (
        re.search(rf"(?m)^\s*namespace:\s*{re.escape(namespace)}\s*$", content)
        is not None
    )


def _namespace_from_kustomization(kustomization: Path) -> Optional[str]:
    if kustomization in KUSTOMIZATION_NAMESPACE_CACHE:
        return KUSTOMIZATION_NAMESPACE_CACHE[kustomization]

    namespace = None
    content = _read_text_if_exists(kustomization)
    if content:
        match = re.search(r"(?m)^\s*namespace:\s*([A-Za-z0-9._-]+)\s*$", content)
        if match:
            namespace = match.group(1)

    KUSTOMIZATION_NAMESPACE_CACHE[kustomization] = namespace
    return namespace


def _cluster_namespace_repo_env_dirs(
    cluster: str,
) -> Dict[str, Dict[str, List[Path]]]:
    if cluster in CLUSTER_NAMESPACE_REPO_ENV_CACHE:
        return CLUSTER_NAMESPACE_REPO_ENV_CACHE[cluster]

    index: Dict[str, Dict[str, List[Path]]] = {}
    for repo_name, repo_dir in _local_kustomize_repos():
        env_root = repo_dir / "environments" / cluster
        if not env_root.exists():
            continue

        for kustomization in env_root.rglob("kustomization.yaml"):
            namespace = _namespace_from_kustomization(kustomization)
            if not namespace:
                relative_parts = kustomization.parent.relative_to(env_root).parts
                namespace = relative_parts[0] if relative_parts else None
            if not namespace:
                continue
            index.setdefault(namespace, {}).setdefault(repo_name, []).append(
                kustomization.parent
            )

    for repo_envs in index.values():
        for env_dirs in repo_envs.values():
            env_dirs.sort()

    CLUSTER_NAMESPACE_REPO_ENV_CACHE[cluster] = index
    return index


def _repo_env_dirs(repo_dir: Path, cluster: str, namespace: str) -> List[Path]:
    cache_key = (str(repo_dir), cluster, namespace)
    if cache_key in REPO_ENV_DIR_CACHE:
        return REPO_ENV_DIR_CACHE[cache_key]

    env_dirs = list(
        _cluster_namespace_repo_env_dirs(cluster)
        .get(namespace, {})
        .get(repo_dir.name, [])
    )
    REPO_ENV_DIR_CACHE[cache_key] = env_dirs
    return env_dirs


def _render_env_dir(env_dir: Path) -> Optional[str]:
    if env_dir in ENV_RENDER_CACHE:
        return ENV_RENDER_CACHE[env_dir]

    commands = [
        ["oc", "kustomize", str(env_dir)],
        ["kustomize", "build", str(env_dir)],
        ["kustomize", "build", "--enable-helm", str(env_dir)],
    ]
    for command in commands:
        try:
            result = run_cmd(command, check=False)
        except FileNotFoundError:
            continue
        if result.returncode == 0 and result.stdout.strip():
            ENV_RENDER_CACHE[env_dir] = result.stdout
            return result.stdout

    ENV_RENDER_CACHE[env_dir] = None
    return None


def _yaml_documents(text: str) -> List[str]:
    docs: List[str] = []
    current: List[str] = []
    for line in text.splitlines():
        if line.strip() == "---":
            if current:
                docs.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        docs.append("\n".join(current))
    return docs


def _manifest_kind_and_name(document: str) -> Tuple[Optional[str], Optional[str]]:
    kind = None
    name = None
    in_metadata = False
    metadata_indent = 0

    for line in document.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        if kind is None and stripped.startswith("kind:"):
            kind = stripped.split(":", 1)[1].strip().strip("'\"")
            continue

        if stripped == "metadata:":
            in_metadata = True
            metadata_indent = indent
            continue

        if in_metadata:
            if indent <= metadata_indent and stripped:
                in_metadata = False
                continue
            if stripped.startswith("name:"):
                name = stripped.split(":", 1)[1].strip().strip("'\"")
                break

    return kind, name


def _rendered_env_matchers(rendered: str) -> Tuple[Set[str], Set[str]]:
    exact_names: Set[str] = set()
    prefixes: Set[str] = set()

    for document in _yaml_documents(rendered):
        kind, name = _manifest_kind_and_name(document)
        if not kind or not name:
            continue

        if kind in WORKLOAD_KINDS:
            exact_names.add(name)
            continue

        if kind == "Kafka":
            exact_names.update(
                {
                    f"{name}-entity-operator",
                    f"{name}-kafka-exporter",
                    f"{name}-cruise-control",
                }
            )
            continue

        if "Postgres" in kind or kind in {"PGCluster", "Postgresql"}:
            exact_names.add(f"{name}-repo-host")
            prefixes.add(f"{name}-{name}-")

    return exact_names, prefixes


def _fallback_env_matchers(env_dir: Path) -> Tuple[Set[str], Set[str]]:
    exact_names: Set[str] = set()

    kustomization = _read_text_if_exists(env_dir / "kustomization.yaml")
    if not kustomization:
        return exact_names, set()

    replica_names = re.findall(
        r"(?m)^\s*-\s+name:\s*([A-Za-z0-9._-]+)\s*$", kustomization
    )
    exact_names.update(replica_names)

    return exact_names, set()


def _env_matchers(env_dir: Path) -> Tuple[Set[str], Set[str]]:
    if env_dir in ENV_MATCHER_CACHE:
        return ENV_MATCHER_CACHE[env_dir]

    rendered = _render_env_dir(env_dir)
    if rendered:
        matchers = _rendered_env_matchers(rendered)
    else:
        matchers = _fallback_env_matchers(env_dir)

    ENV_MATCHER_CACHE[env_dir] = matchers
    return matchers


def _deployment_matches_env_dir(env_dir: Path, deployment: str) -> bool:
    exact_names, prefixes = _env_matchers(env_dir)
    if deployment in exact_names:
        return True
    if find_resource_patch_file(env_dir, target_deployment=deployment):
        return True
    return any(deployment.startswith(prefix) for prefix in prefixes)


def find_repo_env_dir(
    repo_dir: Path, cluster: str, namespace: str, deployment: str
) -> Optional[Path]:
    standard_env_dir = repo_dir / "environments" / cluster / namespace
    if standard_env_dir.exists() and _deployment_matches_env_dir(
        standard_env_dir, deployment
    ):
        return standard_env_dir

    for env_dir in _repo_env_dirs(repo_dir, cluster, namespace):
        if env_dir == standard_env_dir:
            continue
        if _deployment_matches_env_dir(env_dir, deployment):
            return env_dir

    return None


def find_limits_file_for_deployment(env_dir: Path, deployment: str) -> Optional[Path]:
    limits_file = find_resource_patch_file(env_dir, target_deployment=deployment)
    if limits_file:
        return limits_file
    if _deployment_matches_env_dir(env_dir, deployment):
        return find_resource_patch_file(env_dir)
    return None


def resolve_repo_for_deployment(
    ns_mapping: Dict[str, str], deployment: str
) -> Optional[str]:
    deploy_base = deployment.replace("-deployment", "")
    repo = (
        ns_mapping.get(deployment) or ns_mapping.get(deploy_base) or ns_mapping.get("*")
    )
    if repo:
        return repo

    best_repo = None
    best_prefix_length = -1
    for key, mapped_repo in ns_mapping.items():
        if not is_prefix_key(key):
            continue
        prefix = _extract_prefix(key)
        if deployment == prefix or deployment.startswith(f"{prefix}-"):
            if len(prefix) > best_prefix_length:
                best_repo = mapped_repo
                best_prefix_length = len(prefix)
    if best_repo:
        return best_repo

    for key, mapped_repo in ns_mapping.items():
        if is_prefix_key(key):
            continue
        if key.endswith(deploy_base):
            return mapped_repo

    return None


def build_ns_repo_map(
    env: str, target_namespaces: List[str]
) -> Dict[str, Dict[str, str]]:
    mapping = {}
    cluster = get_cluster(env)

    for ns in target_namespaces:
        mapping[ns] = {}
        apps_template = os.getenv(
            "CLUSTER_CONFIG_APPS_TEMPLATE",
            "namespaces/{cluster}/{namespace}/applications/kustomization.yaml",
        )
        apps_file = CLUSTER_CONFIG / apps_template.format(cluster=cluster, namespace=ns)
        app_repos = (
            set()
            if _is_monorepo_layout()
            else set(_repo_names_from_apps_file(apps_file))
        )
        namespace_repo_envs = _cluster_namespace_repo_env_dirs(cluster).get(ns, {})

        for repo_name in sorted(app_repos):
            _register_repo_aliases(mapping[ns], repo_name, prefer=True)

        for repo_name, env_dirs in namespace_repo_envs.items():
            prefer = repo_name in app_repos
            _register_repo_aliases(mapping[ns], repo_name, prefer=prefer)

            for env_dir in env_dirs:
                exact_names, prefixes = _env_matchers(env_dir)
                for workload_name in exact_names:
                    _register_mapping(
                        mapping[ns], workload_name, repo_name, prefer=prefer
                    )
                for prefix in prefixes:
                    _register_mapping(
                        mapping[ns], _prefix_key(prefix), repo_name, prefer=prefer
                    )
    return mapping


def find_resource_patch_file(
    env_dir: Path, target_deployment: Optional[str] = None
) -> Optional[Path]:
    # First, collect all candidate patch files referenced in kustomization.yaml
    candidate_patches = []

    def add_candidate(path: Path) -> None:
        if path not in candidate_patches:
            candidate_patches.append(path)

    kustomization = env_dir / "kustomization.yaml"
    if kustomization.exists():
        content = kustomization.read_text()

        # Check patchesStrategicMerge
        in_section = False
        for line in content.splitlines():
            if line.startswith("patchesStrategicMerge:"):
                in_section = True
                continue
            if (
                in_section
                and line
                and not line.startswith(" ")
                and not line.startswith("-")
            ):
                in_section = False
            if in_section and line.strip().startswith("-"):
                val = line.strip()[1:].strip().strip("'\"")
                add_candidate(env_dir / val)

        # Check patches: - path: syntax
        in_patches_section = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "patches:":
                in_patches_section = True
                continue
            if (
                in_patches_section
                and line
                and not line.startswith(" ")
                and not line.startswith("-")
            ):
                in_patches_section = False
            if in_patches_section and stripped.startswith("- path:"):
                val = stripped.split("- path:")[1].strip().strip("'\"")
                add_candidate(env_dir / val)
            elif in_patches_section and stripped.startswith("path:"):
                val = stripped.split("path:", 1)[1].strip().strip("'\"")
                add_candidate(env_dir / val)

    # Also check common resource patch filenames as fallback candidates. Some
    # repos keep these files in predictable names while the kustomization uses a
    # shape that this lightweight parser cannot fully interpret.
    for fallback_name in (
        "limits-patch.yaml",
        "resource-limits.yaml",
        "deployment-patch.yaml",
        "deployment-patches.yaml",
    ):
        fallback_patch = env_dir / fallback_name
        if fallback_patch.exists():
            add_candidate(fallback_patch)

    resource_patch_candidates: List[Tuple[Path, str]] = []

    for candidate in candidate_patches:
        if not candidate.exists():
            continue

        content = candidate.read_text()
        has_resource_block = (
            re.search(r"(?m)^\s*requests:\s*$", content) is not None
            and re.search(r"(?m)^\s*limits:\s*$", content) is not None
            and re.search(r"(?m)^\s*cpu:\s*.+$", content) is not None
            and re.search(r"(?m)^\s*memory:\s*.+$", content) is not None
        )
        if not has_resource_block:
            continue

        resource_patch_candidates.append((candidate, content))

    if target_deployment:
        for candidate, content in resource_patch_candidates:
            # If target provided, ensure the patch file name or its content targets the deployment.
            if re.search(
                rf"name:\s*['\"]?{re.escape(target_deployment)}['\"]?(?:\s|$)", content
            ):
                return candidate

            # Substring match if exact match fails
            target_base = target_deployment.replace("-deployment", "")
            if re.search(
                rf"name:\s*['\"]?{re.escape(target_base)}['\"]?(?:\s|$)", content
            ) or re.search(rf"\b{re.escape(target_base)}\b", candidate.name):
                return candidate

        return None

    for preferred_name in (
        "limits-patch.yaml",
        "resource-limits.yaml",
        "deployment-patch.yaml",
        "deployment-patches.yaml",
    ):
        for candidate, _content in resource_patch_candidates:
            if candidate.name == preferred_name:
                return candidate

    if resource_patch_candidates:
        return resource_patch_candidates[0][0]

    return None


def read_limits_file(path: Optional[Path]) -> Tuple[str, str, str, str]:
    """Helper to parse a patch file and extract current resource limits and requests."""
    if not path or not path.exists():
        return "not set", "not set", "not set", "not set"

    lines = path.read_text().splitlines()
    req_cpu, req_mem, lim_cpu, lim_mem = "not set", "not set", "not set", "not set"
    current_section = None

    for line in lines:
        stripped = line.strip()
        if stripped == "limits:":
            current_section = "limits"
        elif stripped == "requests:":
            current_section = "requests"
        elif current_section == "limits" and stripped.startswith("cpu:"):
            lim_cpu = stripped.split("cpu:")[1].strip().strip("\"'")
        elif current_section == "limits" and stripped.startswith("memory:"):
            lim_mem = stripped.split("memory:")[1].strip().strip("\"'")
        elif current_section == "requests" and stripped.startswith("cpu:"):
            req_cpu = stripped.split("cpu:")[1].strip().strip("\"'")
        elif current_section == "requests" and stripped.startswith("memory:"):
            req_mem = stripped.split("memory:")[1].strip().strip("\"'")

    return req_cpu, req_mem, lim_cpu, lim_mem


def update_limits_file(
    path: Path, request_cpu: str, request_mem: str, limit_cpu: str, limit_mem: str
):
    lines = path.read_text().splitlines()
    in_containers, target_container = False, False
    current_section, resources_indent = None, None
    replaced = {
        "limits_cpu": False,
        "limits_memory": False,
        "requests_cpu": False,
        "requests_memory": False,
    }

    for index, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if stripped == "containers:":
            in_containers, target_container, current_section, resources_indent = (
                True,
                False,
                None,
                None,
            )
            continue
        if in_containers and stripped == "initContainers:":
            break
        if in_containers and re.match(r"-\s+name:\s+", stripped):
            if not target_container:
                target_container = True
            else:
                break
            current_section, resources_indent = None, None
            continue
        if not target_container:
            continue
        if stripped == "resources:":
            resources_indent, current_section = indent, None
            continue
        if resources_indent is not None and indent <= resources_indent and stripped:
            current_section = None
        if stripped == "limits:":
            current_section = "limits"
            continue
        if stripped == "requests:":
            current_section = "requests"
            continue

        if current_section == "limits" and stripped.startswith("cpu:"):
            lines[index] = re.sub(r"cpu:\s*.*$", f'cpu: "{limit_cpu}"', line)
            replaced["limits_cpu"] = True
        elif current_section == "limits" and stripped.startswith("memory:"):
            lines[index] = re.sub(r"memory:\s*.*$", f'memory: "{limit_mem}"', line)
            replaced["limits_memory"] = True
        elif current_section == "requests" and stripped.startswith("cpu:"):
            lines[index] = re.sub(r"cpu:\s*.*$", f'cpu: "{request_cpu}"', line)
            replaced["requests_cpu"] = True
        elif current_section == "requests" and stripped.startswith("memory:"):
            lines[index] = re.sub(r"memory:\s*.*$", f'memory: "{request_mem}"', line)
            replaced["requests_memory"] = True

    missing = [name for name, done in replaced.items() if not done]
    if missing:
        raise ValueError(
            f"Missing expected resource keys in {path}: {', '.join(missing)}"
        )

    path.write_text("\n".join(lines) + "\n")

def update_limits(
    repo: str,
    ns: str,
    deployment: str,
    request_cpu: str,
    request_mem: str,
    limit_cpu_recommendation: str,
    limit_mem_recommendation: str,
    cluster: str,
) -> str:
    repo_dir = REPOS_DIR / repo
    env_dir = find_repo_env_dir(repo_dir, cluster, ns, deployment)
    if not env_dir:
        raise FileNotFoundError(
            f"Could not find kustomization for {deployment} in {ns}"
        )
    limits_file = find_limits_file_for_deployment(env_dir, deployment)

    if not limits_file:
        raise FileNotFoundError(
            f"Could not find a resource patch file via {env_dir}/kustomization.yaml"
        )

    # Burstable QoS: limits allow bursting above requests.
    limit_cpu = limit_cpu_recommendation
    limit_mem = limit_mem_recommendation

    update_limits_file(limits_file, request_cpu, request_mem, limit_cpu, limit_mem)
    return (
        f"file: {limits_file.name}; requests: ->{request_cpu}, ->{request_mem}; "
        f"limits (Burstable QoS): ->{limit_cpu}, ->{limit_mem}"
    )
