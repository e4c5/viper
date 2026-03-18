"""GitHub API provider (for local testing without Gitea)."""

import base64
import logging
from typing import Any

import httpx

from code_review.providers.base import (
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    ProviderInterface,
    ReviewComment,
    _log_pr_info_warning,
    file_infos_from_pull_file_list,
    pr_info_from_api_dict,
)
from code_review.formatters.comment import render_suggestion_block
from code_review.providers.safety import truncate_repo_content

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
DEFAULT_BASE_URL = "https://api.github.com"
logger = logging.getLogger(__name__)


class GitHubProvider(ProviderInterface):
    """GitHub API client for PR diff, file content, and review comments."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str = "", timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "Accept": "application/vnd.github+json",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(url, headers=self._headers(), params=params or {})
            r.raise_for_status()
            if r.headers.get("content-type", "").startswith("application/json"):
                return r.json()
            return r.text

    def _get_diff(self, path: str) -> str:
        """GET with Accept application/vnd.github.v3.diff for unified diff."""
        url = f"{self._base_url}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(
                url,
                headers={**self._headers(), "Accept": "application/vnd.github.v3.diff"},
            )
            r.raise_for_status()
            return r.text

    def _post(self, path: str, json: Any) -> Any:
        url = f"{self._base_url}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(url, headers=self._headers(), json=json)
            r.raise_for_status()
            return r.json() if r.content else None

    def _patch(self, path: str, json: Any) -> Any:
        url = f"{self._base_url}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            r = client.patch(url, headers=self._headers(), json=json)
            r.raise_for_status()
            return r.json() if r.content else None

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff for the PR (Accept: application/vnd.github.v3.diff)."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        return self._get_diff(path)

    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Return file content at ref; truncated if over max size."""
        api_path = f"/repos/{owner}/{repo}/contents/{path}"
        resp = self._get(api_path, params={"ref": ref})
        if isinstance(resp, dict) and "content" in resp:
            raw = base64.b64decode(resp["content"]).decode("utf-8", errors="replace")
            return truncate_repo_content(raw, max_bytes=MAX_REPO_FILE_BYTES)
        raise ValueError(f"Unexpected response for {path} at {ref}")

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        """Return list of changed files in the PR."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
        data = self._get(path, params={"per_page": 100})
        return file_infos_from_pull_file_list(data) if isinstance(data, list) else []

    def post_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        comments: list[InlineComment],
        head_sha: str = "",
    ) -> None:
        """Post inline review comments via Create a review (event COMMENT, comments array)."""
        if not comments:
            return
        review_comments = [
            {
                "path": c.path,
                "side": "RIGHT",
                "body": render_suggestion_block(c.body, c.suggested_patch),
                **(
                    {"start_line": c.line, "line": c.end_line}
                    if (c.end_line is not None and c.end_line != c.line)
                    else {"line": c.line}
                ),
            }
            for c in comments
        ]
        payload: dict[str, Any] = {
            "event": "COMMENT",
            "body": "Code review comments",
            "comments": review_comments,
        }
        if head_sha:
            payload["commit_id"] = head_sha
        self._post(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", payload)

    def get_existing_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewComment]:
        """Return existing review comments. GitHub does not expose 'resolved' on list."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        data = self._get(path, params={"per_page": 100})
        if not isinstance(data, list):
            return []
        result: list[ReviewComment] = []
        for c in data:
            result.append(
                ReviewComment(
                    id=str(c.get("id", "")),
                    path=c.get("path", ""),
                    line=int(c.get("line", 0) or 0),
                    body=c.get("body", ""),
                    resolved=False,
                )
            )
        return result

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post PR-level comment (GitHub: issues comments endpoint for PRs)."""
        self._post(f"/repos/{owner}/{repo}/issues/{pr_number}/comments", {"body": body})

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """Return PR title, labels, and description for skip-review and metadata."""
        try:
            data = self._get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
            return pr_info_from_api_dict(data, "body") if isinstance(data, dict) else None
        except Exception as e:
            _log_pr_info_warning(logger, owner, repo, pr_number, e)
            return None

    def update_pr_description(
        self, owner: str, repo: str, pr_number: int, description: str, title: str | None = None
    ) -> None:
        """Update the PR body (and optionally title) via PATCH /repos/.../pulls/{number}."""
        payload: dict[str, str] = {"body": description}
        if title is not None:
            payload["title"] = title
        self._patch(f"/repos/{owner}/{repo}/pulls/{pr_number}", payload)

    def capabilities(self) -> ProviderCapabilities:
        """GitHub supports suggestion blocks; resolved is per-conversation, not per-comment."""
        return ProviderCapabilities(resolvable_comments=False, supports_suggestions=True)
