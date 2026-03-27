#!/usr/bin/env python3
"""Create a Bitbucket Server/Data Center pull request for a local instance.

Usage:
  python scripts/create_bitbucket_pull_request.py <project> <repo> <source_branch> <destination_branch>

Authentication:
  SE_USER and SE_PASSWORD are read from the environment; for local repo usage
  this script also auto-loads the repo-root .env when present.

Bitbucket base URL is assumed to be http://localhost:7990 and this script targets
the REST API base at http://localhost:7990/rest/api/1.0.
"""

from __future__ import annotations

import argparse
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
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


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
    url = (
        f"{BITBUCKET_REST_API_BASE}/projects/{urllib.parse.quote(project_key, safe='')}"
        f"/repos/{urllib.parse.quote(repo_slug, safe='')}/pull-requests"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": auth_header(username, password),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            detail = json.dumps(parsed, indent=2)
        except json.JSONDecodeError:
            detail = body
        raise RuntimeError(f"Bitbucket returned HTTP {exc.code}:\n{detail}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a Bitbucket Server/DC pull request on http://localhost:7990."
    )
    parser.add_argument("project_key", help="Bitbucket project key, for example PRJ")
    parser.add_argument("repo_slug", help="Bitbucket repository slug")
    parser.add_argument("source_branch", help="Source branch name")
    parser.add_argument("destination_branch", help="Destination branch name")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    load_local_env()
    username = os.environ.get("SE_USER", "").strip()
    password = os.environ.get("SE_PASSWORD", "").strip()
    if not username or not password:
        parser.error("SE_USER and SE_PASSWORD must be set (or present in the repo .env file).")

    result = create_pull_request(
        args.project_key,
        args.repo_slug,
        args.source_branch,
        args.destination_branch,
        username=username,
        password=password,
    )

    pr_id = result.get("id", "<unknown>")
    title = result.get("title", "")
    links = result.get("links", {})
    self_links = links.get("self") if isinstance(links, dict) else None
    pr_url = ""
    if isinstance(self_links, list) and self_links:
        first = self_links[0]
        if isinstance(first, dict):
            pr_url = str(first.get("href", "")).strip()

    print(f"Created pull request #{pr_id}: {title}")
    if pr_url:
        print(pr_url)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
