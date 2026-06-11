import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

from resource_updater.models import BitbucketAuth
from resource_updater.utils import log_warn
from resource_updater.config import WORKSPACE


def get_bitbucket_auth() -> Optional[BitbucketAuth]:
    email = os.getenv("BITBUCKET_EMAIL", "").strip()
    api_token = os.getenv("BITBUCKET_API_TOKEN", "").strip()
    username = os.getenv("BITBUCKET_USERNAME", "").strip()
    app_password = os.getenv("BITBUCKET_APP_PASSWORD", "").strip()

    if email and api_token:
        return BitbucketAuth(email, api_token, "api_token")

    if username and app_password:
        return BitbucketAuth(username, app_password, "app_password")

    return None


def _bitbucket_auth_requirements() -> str:
    return (
        "Provide either BITBUCKET_EMAIL + BITBUCKET_API_TOKEN "
        "or BITBUCKET_USERNAME + BITBUCKET_APP_PASSWORD. "
        "To create, merge, or decline PRs with an API token, it also needs "
        "the Pull requests: Write scope."
    )


def validate_bitbucket_credentials() -> None:
    if get_bitbucket_auth() is None:
        raise RuntimeError(
            f"Missing Bitbucket credentials. {_bitbucket_auth_requirements()}"
        )


def _bb_api_request(
    method: str, url: str, data: Optional[dict] = None, max_retries: int = 5
) -> dict:
    auth = get_bitbucket_auth()
    if auth is None:
        raise RuntimeError(
            f"Missing Bitbucket credentials. {_bitbucket_auth_requirements()}"
        )

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, method=method)
    auth_str = f"{auth.principal}:{auth.secret}"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()
    req.add_header("Authorization", f"Basic {auth_b64}")
    req.add_header("Accept", "application/json")

    if data is not None:
        req.add_header("Content-Type", "application/json")
        req_data = json.dumps(data).encode("utf-8")
    else:
        req_data = None

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, data=req_data, context=ctx) as response:
                if response.status == 204:
                    return {}
                return json.loads(response.read().decode("utf-8"))

        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_time = int(retry_after)
                else:
                    wait_time = 2**attempt

                log_warn(
                    f"Bitbucket API rate limit reached (429). Retrying in {wait_time}s... (Attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait_time)
                continue

            try:
                error_info = json.loads(e.read().decode("utf-8"))
                msg = error_info.get("error", {}).get("message", "Unknown error")
            except (json.JSONDecodeError, UnicodeDecodeError):
                msg = e.reason
            raise RuntimeError(f"API Error {e.code}: {msg}") from e

    raise RuntimeError(
        f"Bitbucket API failed after {max_retries} retries due to rate limiting."
    )


def repo_slug(reponame: str) -> str:
    prefix = os.getenv("REPO_PREFIX", "")
    return f"{prefix}{reponame}"


def repo_remote_url(reponame: str) -> str:
    return f"git@bitbucket.org:{WORKSPACE}/{repo_slug(reponame)}.git"


def create_pull_request(
    repo: str, base_branch: str, work_branch: str, title: str, desc: str
) -> Optional[Tuple[int, str]]:
    base_url = f"https://api.bitbucket.org/2.0/repositories/{WORKSPACE}/{repo_slug(repo)}/pullrequests"

    # Check for existing PR
    try:
        query_params = urllib.parse.urlencode(
            {"q": f'source.branch.name="{work_branch}" AND state="OPEN"'}
        )
        search_url = f"{base_url}?{query_params}"
        search_res = _bb_api_request("GET", search_url)
        if search_res.get("values"):
            existing_pr = search_res["values"][0]
            pr_id = existing_pr.get("id")
            pr_url = existing_pr.get("links", {}).get("html", {}).get("href")
            if isinstance(pr_id, int) and isinstance(pr_url, str):
                try:
                    # Add comment to existing PR
                    comment_url = f"{base_url}/{pr_id}/comments"
                    _bb_api_request(
                        "POST",
                        comment_url,
                        {"content": {"raw": "Updated with latest Prometheus metrics."}},
                    )
                except (RuntimeError, urllib.error.URLError):
                    pass
                return pr_id, pr_url
    except (RuntimeError, urllib.error.URLError, KeyError, TypeError):
        pass

    payload = {
        "title": title,
        "description": desc,
        "source": {"branch": {"name": work_branch}},
        "destination": {"branch": {"name": base_branch}},
    }

    try:
        res = _bb_api_request("POST", base_url, payload)
        pr_id = res.get("id")
        pr_url = res.get("links", {}).get("html", {}).get("href")
        if not isinstance(pr_id, int) or not isinstance(pr_url, str) or not pr_url:
            raise RuntimeError(f"Bitbucket returned an unexpected PR response: {res}")
        return pr_id, pr_url
    except (RuntimeError, urllib.error.URLError) as e:
        if "API Error 401" in str(e):
            log_warn(
                "Bitbucket accepted neither the current write credentials nor scopes. "
                f"{_bitbucket_auth_requirements()}"
            )
        log_warn(f"Failed to create PR for repo: {repo}. Reason: {e}")
        return None


def merge_pull_request(repo: str, pr_id: int):
    url = f"https://api.bitbucket.org/2.0/repositories/{WORKSPACE}/{repo_slug(repo)}/pullrequests/{pr_id}/merge"
    _bb_api_request("POST", url, data={})


def decline_pull_request(repo: str, pr_id: int):
    url = f"https://api.bitbucket.org/2.0/repositories/{WORKSPACE}/{repo_slug(repo)}/pullrequests/{pr_id}/decline"
    _bb_api_request("POST", url, data={})
