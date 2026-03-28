"""Shared helpers for local Bitbucket Server/Data Center pull request scripts."""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from local_env import load_local_env

BITBUCKET_BASE_URL = "http://localhost:7990"
BITBUCKET_REST_API_BASE = f"{BITBUCKET_BASE_URL}/rest/api/1.0"


def load_script_credentials() -> tuple[str, str]:
    """Load repo-local env values and return Bitbucket credentials."""
    load_local_env()
    username = os.environ.get("SE_USER", "").strip()
    password = os.environ.get("SE_PASSWORD", "").strip()
    return username, password


def branch_ref(branch_name: str) -> str:
    """Return a fully-qualified Bitbucket branch ref."""
    if branch_name.startswith("refs/"):
        return branch_name
    return f"refs/heads/{branch_name}"


def build_pr_payload(
    project_key: str,
    repo_slug: str,
    source_branch: str,
    destination_branch: str,
) -> dict[str, Any]:
    """Build the minimal Bitbucket Server PR creation payload."""
    title = f"{source_branch} -> {destination_branch}"
    repo_obj = {
        "slug": repo_slug,
        "project": {
            "key": project_key,
        },
    }
    return {
        "title": title,
        "description": "",
        "fromRef": {
            "id": branch_ref(source_branch),
            "repository": repo_obj,
        },
        "toRef": {
            "id": branch_ref(destination_branch),
            "repository": repo_obj,
        },
    }


def auth_header(username: str, password: str) -> str:
    """Build a Basic auth header for local Bitbucket scripts."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def pull_requests_url(project_key: str, repo_slug: str, pull_request_id: int | str | None = None) -> str:
    """Return the REST URL for the repository's pull requests collection or one PR."""
    base_url = (
        f"{BITBUCKET_REST_API_BASE}/projects/{urllib.parse.quote(project_key, safe='')}"
        f"/repos/{urllib.parse.quote(repo_slug, safe='')}/pull-requests"
    )
    if pull_request_id is None:
        return base_url
    return f"{base_url}/{urllib.parse.quote(str(pull_request_id), safe='')}"


def pull_request_comments_url(
    project_key: str,
    repo_slug: str,
    pull_request_id: int | str,
    comment_id: int | str | None = None,
    *,
    activities: bool = False,
) -> str:
    """Return the REST URL for PR comments or activities."""
    base_url = pull_requests_url(project_key, repo_slug, pull_request_id)
    if activities:
        return f"{base_url}/activities"
    comments_url = f"{base_url}/comments"
    if comment_id is None:
        return comments_url
    return f"{comments_url}/{urllib.parse.quote(str(comment_id), safe='')}"


def pull_request_tasks_url(
    project_key: str,
    repo_slug: str,
    pull_request_id: int | str,
) -> str:
    """Return the REST URL for pull request tasks."""
    return f"{pull_requests_url(project_key, repo_slug, pull_request_id)}/tasks"


def bitbucket_request(
    method: str,
    url: str,
    *,
    username: str,
    password: str,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """Send an authenticated Bitbucket request and decode JSON responses when present."""
    if params:
        query = urllib.parse.urlencode([(key, str(value)) for key, value in params.items()])
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{query}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": auth_header(username, password),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read()
            if not body:
                return None
            text = body.decode("utf-8")
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type or text.lstrip().startswith(("{", "[")):
                return json.loads(text)
            return text
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            detail = json.dumps(parsed, indent=2)
        except json.JSONDecodeError:
            detail = body
        raise RuntimeError(f"Bitbucket returned HTTP {exc.code}:\n{detail}") from exc


def create_pull_request(
    project_key: str,
    repo_slug: str,
    source_branch: str,
    destination_branch: str,
    *,
    username: str,
    password: str,
) -> dict[str, Any]:
    """Create the pull request and return the decoded response payload."""
    payload = build_pr_payload(project_key, repo_slug, source_branch, destination_branch)
    result = bitbucket_request(
        "POST",
        pull_requests_url(project_key, repo_slug),
        username=username,
        password=password,
        payload=payload,
    )
    if not isinstance(result, dict):
        raise RuntimeError("Bitbucket returned an unexpected response while creating the pull request.")
    return result


def get_pull_request(
    project_key: str,
    repo_slug: str,
    pull_request_id: int | str,
    *,
    username: str,
    password: str,
) -> dict[str, Any]:
    """Fetch one pull request from Bitbucket."""
    result = bitbucket_request(
        "GET",
        pull_requests_url(project_key, repo_slug, pull_request_id),
        username=username,
        password=password,
    )
    if not isinstance(result, dict):
        raise RuntimeError("Bitbucket returned an unexpected response while fetching the pull request.")
    return result


def get_pull_request_comment(
    project_key: str,
    repo_slug: str,
    pull_request_id: int | str,
    comment_id: int | str,
    *,
    username: str,
    password: str,
) -> dict[str, Any]:
    """Fetch one pull request comment from Bitbucket."""
    result = bitbucket_request(
        "GET",
        pull_request_comments_url(project_key, repo_slug, pull_request_id, comment_id),
        username=username,
        password=password,
    )
    if not isinstance(result, dict):
        raise RuntimeError("Bitbucket returned an unexpected response while fetching the pull request comment.")
    return result


def _next_page_start(page: dict[str, Any], current_start: int) -> int | None:
    """Return the next page start for Bitbucket paged responses."""
    if bool(page.get("isLastPage", True)):
        return None
    next_start = page.get("nextPageStart")
    if next_start is None:
        return None
    try:
        next_value = int(next_start)
    except (TypeError, ValueError):
        return None
    if next_value == current_start:
        return None
    return next_value


def list_pull_request_comments(
    project_key: str,
    repo_slug: str,
    pull_request_id: int | str,
    *,
    username: str,
    password: str,
) -> list[dict[str, Any]]:
    """List pull request comments via the activities endpoint for compatibility."""
    comments: list[dict[str, Any]] = []
    start = 0
    seen_starts: set[int] = set()
    max_pages = 500
    for _ in range(max_pages):
        if start in seen_starts:
            raise RuntimeError(
                f"Bitbucket comment pagination cycle detected while listing pull request comments: "
                f"repeated start={start}."
            )
        seen_starts.add(start)
        result = bitbucket_request(
            "GET",
            pull_request_comments_url(project_key, repo_slug, pull_request_id, activities=True),
            username=username,
            password=password,
            params={"start": start, "limit": 100},
        )
        if not isinstance(result, dict):
            raise RuntimeError("Bitbucket returned an unexpected response while listing pull request comments.")
        for activity in result.get("values") or []:
            if not isinstance(activity, dict):
                continue
            if str(activity.get("action") or "").upper() != "COMMENTED":
                continue
            comment = activity.get("comment")
            if isinstance(comment, dict):
                merged = dict(comment)
                anchor = merged.get("anchor")
                if not isinstance(anchor, dict):
                    activity_anchor = activity.get("commentAnchor")
                    if isinstance(activity_anchor, dict):
                        merged["anchor"] = activity_anchor
                comments.append(merged)
        next_start = _next_page_start(result, start)
        if next_start is None:
            break
        start = next_start
    else:
        raise RuntimeError(
            f"Bitbucket comment pagination exceeded max_pages={max_pages} while listing "
            "pull request comments."
    )
    return comments


def list_pull_request_activities(
    project_key: str,
    repo_slug: str,
    pull_request_id: int | str,
    *,
    username: str,
    password: str,
) -> list[dict[str, Any]]:
    """List raw pull request activities via the paginated activities endpoint."""
    activities: list[dict[str, Any]] = []
    start = 0
    seen_starts: set[int] = set()
    max_pages = 500
    for _ in range(max_pages):
        if start in seen_starts:
            raise RuntimeError(
                "Bitbucket activity pagination cycle detected while listing pull request activities: "
                f"repeated start={start}."
            )
        seen_starts.add(start)
        result = bitbucket_request(
            "GET",
            pull_request_comments_url(project_key, repo_slug, pull_request_id, activities=True),
            username=username,
            password=password,
            params={"start": start, "limit": 100},
        )
        if not isinstance(result, dict):
            raise RuntimeError(
                "Bitbucket returned an unexpected response while listing pull request activities."
            )
        for activity in result.get("values") or []:
            if isinstance(activity, dict):
                activities.append(activity)
        next_start = _next_page_start(result, start)
        if next_start is None:
            break
        start = next_start
    else:
        raise RuntimeError(
            f"Bitbucket activity pagination exceeded max_pages={max_pages} while listing "
            "pull request activities."
        )
    return activities


def pull_request_version(pull_request: dict[str, Any]) -> int:
    """Return the integer version required by Bitbucket's delete PR endpoint."""
    version = pull_request.get("version")
    if isinstance(version, int):
        return version
    if isinstance(version, str):
        try:
            return int(version)
        except ValueError:
            pass
    raise RuntimeError("Bitbucket pull request payload did not include a valid integer 'version'.")


def delete_pull_request(
    project_key: str,
    repo_slug: str,
    pull_request_id: int | str,
    *,
    username: str,
    password: str,
) -> dict[str, Any]:
    """Delete a pull request after fetching its current version."""
    pull_request = get_pull_request(
        project_key,
        repo_slug,
        pull_request_id,
        username=username,
        password=password,
    )
    bitbucket_request(
        "DELETE",
        pull_requests_url(project_key, repo_slug, pull_request_id),
        username=username,
        password=password,
        payload={"version": pull_request_version(pull_request)},
    )
    return pull_request


def delete_pull_request_comment(
    project_key: str,
    repo_slug: str,
    pull_request_id: int | str,
    comment_id: int | str,
    *,
    username: str,
    password: str,
) -> dict[str, Any]:
    """Delete a pull request comment after fetching its current version."""
    comment = get_pull_request_comment(
        project_key,
        repo_slug,
        pull_request_id,
        comment_id,
        username=username,
        password=password,
    )
    version = comment.get("version")
    if not isinstance(version, int):
        if isinstance(version, str):
            try:
                version = int(version)
            except ValueError as exc:
                raise RuntimeError(
                    "Bitbucket pull request comment payload did not include a valid integer 'version'."
                ) from exc
        else:
            raise RuntimeError(
                "Bitbucket pull request comment payload did not include a valid integer 'version'."
            )
    bitbucket_request(
        "DELETE",
        pull_request_comments_url(project_key, repo_slug, pull_request_id, comment_id),
        username=username,
        password=password,
        params={"version": version},
    )
    return comment


def list_pull_request_tasks(
    project_key: str,
    repo_slug: str,
    pull_request_id: int | str,
    *,
    username: str,
    password: str,
) -> list[dict[str, Any]]:
    """List pull request tasks via the paginated tasks endpoint."""
    tasks: list[dict[str, Any]] = []
    start = 0
    seen_starts: set[int] = set()
    max_pages = 500
    for _ in range(max_pages):
        if start in seen_starts:
            raise RuntimeError(
                "Bitbucket task pagination cycle detected while listing pull request tasks: "
                f"repeated start={start}."
            )
        seen_starts.add(start)
        result = bitbucket_request(
            "GET",
            pull_request_tasks_url(project_key, repo_slug, pull_request_id),
            username=username,
            password=password,
            params={"start": start, "limit": 100},
        )
        if not isinstance(result, dict):
            raise RuntimeError("Bitbucket returned an unexpected response while listing pull request tasks.")
        for task in result.get("values") or []:
            if isinstance(task, dict):
                tasks.append(task)
        next_start = _next_page_start(result, start)
        if next_start is None:
            break
        start = next_start
    else:
        raise RuntimeError(
            f"Bitbucket task pagination exceeded max_pages={max_pages} while listing pull request tasks."
        )
    return tasks


def extract_pull_request_url(pull_request: dict[str, Any]) -> str:
    """Best-effort extraction of a self link from a Bitbucket PR payload."""
    links = pull_request.get("links", {})
    self_links = links.get("self") if isinstance(links, dict) else None
    if isinstance(self_links, list) and self_links:
        first = self_links[0]
        if isinstance(first, dict):
            return str(first.get("href", "")).strip()
    return ""
