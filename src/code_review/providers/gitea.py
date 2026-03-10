"""Gitea API provider."""

import logging
import time
from typing import Any

import httpx

from code_review.diff.position import get_diff_hunk_for_line
from code_review.providers.base import (
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    ProviderInterface,
    RateLimitError,
    ReviewComment,
    file_infos_from_pull_file_list,
    pr_info_from_api_dict,
)
from code_review.providers.safety import truncate_repo_content

logger = logging.getLogger(__name__)


class GiteaProvider(ProviderInterface):
    """Gitea API client for PR diff, file content, and review comments."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self._token}",
            "Accept": "application/json",
        }

    _RETRY_STATUSES = (502, 503, 504)
    _RETRY_DELAY_SECONDS = 1.0

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        max_retries: int = 1,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform request with one retry on transient server errors (502, 503, 504).

        Rate limit errors (429) are not retried; a RateLimitError is raised
        immediately so callers can skip to the next task rather than making
        the rate limit situation worse.
        """
        with httpx.Client(timeout=self._timeout) as client:
            r = client.request(method, url, headers=self._headers(), **kwargs)
            if r.status_code == 429:
                raise RateLimitError(
                    f"Rate limit exceeded (HTTP 429) for {method} {url}: {r.text}"
                )
            if r.status_code in self._RETRY_STATUSES and max_retries > 0:
                time.sleep(self._RETRY_DELAY_SECONDS)
                r = client.request(method, url, headers=self._headers(), **kwargs)
            return r

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._base_url}/api/v1{path}"
        r = self._request_with_retry("GET", url, params=params)
        r.raise_for_status()
        return (
            r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        )

    def _get_text(self, path: str) -> str:
        url = f"{self._base_url}/api/v1{path}"
        r = self._request_with_retry("GET", url)
        r.raise_for_status()
        return r.text

    def _post(self, path: str, json: Any) -> Any:
        url = f"{self._base_url}/api/v1{path}"
        r = self._request_with_retry("POST", url, json=json)
        r.raise_for_status()
        return r.json() if r.content else None

    def _patch(self, path: str, json: Any) -> Any:
        url = f"{self._base_url}/api/v1{path}"
        r = self._request_with_retry("PATCH", url, json=json)
        r.raise_for_status()
        return r.json() if r.content else None

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff for the PR."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}.diff"
        return self._get_text(path)

    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Return file content at ref; truncated with delimiter if over max size."""
        import base64
        import binascii

        api_path = f"/repos/{owner}/{repo}/contents/{path}"
        resp = self._get(api_path, params={"ref": ref})
        if isinstance(resp, dict) and "content" in resp:
            try:
                decoded = base64.b64decode(resp["content"])
            except (binascii.Error, TypeError) as exc:  # malformed or non-base64 content
                raise ValueError(f"Invalid base64 content for {path} at {ref}") from exc
            raw = decoded.decode("utf-8", errors="replace")
            return truncate_repo_content(raw)
        raise ValueError(f"Unexpected response for {path} at {ref}")

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        """Return list of changed files in the PR."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
        data = self._get(path)
        return file_infos_from_pull_file_list(data) if isinstance(data, list) else []

    def post_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        comments: list[InlineComment],
        head_sha: str = "",
    ) -> None:
        """Post inline review comments. Convert internal InlineComment to Gitea API payload.

        Gitea shows comments in the diff (Files changed) when each comment has valid path,
        new_position (line in new file), and commit_id. We fetch the PR diff and include
        diff_hunk per comment when possible so the UI can pin the comment to the diff section.
        """
        if not comments:
            return
        diff_text: str | None = None
        try:
            diff_text = self.get_pr_diff(owner, repo, pr_number)
        except Exception:
            pass
        review_comments: list[dict[str, Any]] = []
        for c in comments:
            path_norm = (c.path or "").lstrip("/")
            if not path_norm:
                path_norm = c.path or ""
            item: dict[str, Any] = {
                "path": path_norm,
                "body": c.body,
                "old_position": 0,
                "new_position": int(c.line),
            }
            if diff_text:
                hunk = get_diff_hunk_for_line(diff_text, c.path, c.line)
                if hunk:
                    item["diff_hunk"] = hunk
            review_comments.append(item)
        payload: dict[str, Any] = {
            "body": "Code review comments",
            "event": "COMMENT",
            "comments": review_comments,
        }
        if head_sha:
            payload["commit_id"] = head_sha
        self._post(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", payload)

    def get_existing_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewComment]:
        """Return existing review comments. Gitea may not expose 'resolved' via API."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        # Older Gitea versions (e.g. 1.21.x) do not expose this endpoint and return 404.
        # Treat 404 as "no existing review comments" instead of failing the whole run.
        try:
            data = self._get(path)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return []
            raise
        if not isinstance(data, list):
            return []
        result: list[ReviewComment] = []
        for c in data:
            # Gitea PR comments: id, path, line, body; resolved status may be absent
            result.append(
                ReviewComment(
                    id=str(c.get("id", "")),
                    path=c.get("path", ""),
                    line=int(c.get("line", 0) or 0),
                    body=c.get("body", ""),
                    resolved=bool(c.get("resolved", False)),
                )
            )
        return result

    def resolve_comment(self, owner: str, repo: str, comment_id: str) -> None:
        """Mark comment as resolved. Gitea does not support updating PR review comments; no-op."""
        try:
            self._patch(
                f"/repos/{owner}/{repo}/pulls/comments/{comment_id}",
                {"resolved": True},
            )
        except httpx.HTTPStatusError:
            # Gitea API does not support PATCH on PR review comments (typically 404/405)
            # No-op for runtime safety if called despite capabilities() returning False
            pass

    def unresolve_comment(self, owner: str, repo: str, comment_id: str) -> None:
        """Mark comment as unresolved. Gitea does not support updating PR review comments; no-op."""
        try:
            self._patch(
                f"/repos/{owner}/{repo}/pulls/comments/{comment_id}",
                {"resolved": False},
            )
        except httpx.HTTPStatusError:
            # Gitea API does not support PATCH on PR review comments (typically 404/405)
            # No-op for runtime safety if called despite capabilities() returning False
            pass

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post PR-level comment. In Gitea, PRs are issues; use issues comments endpoint."""
        self._post(
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            {"body": body},
        )

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """Return PR title, labels, and description for skip-review and metadata."""
        try:
            data = self._get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
            return pr_info_from_api_dict(data, "body") if isinstance(data, dict) else None
        except Exception as e:
            logger.warning(
                "get_pr_info failed owner=%s repo=%s pr_number=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return None

    def update_pr_description(
        self, owner: str, repo: str, pr_number: int, description: str, title: str | None = None
    ) -> None:
        """Update the PR body (and optionally title) via PATCH /repos/.../pulls/{index}."""
        payload: dict[str, str] = {"body": description}
        if title is not None:
            payload["title"] = title
        self._patch(f"/repos/{owner}/{repo}/pulls/{pr_number}", payload)

    def capabilities(self) -> ProviderCapabilities:
        """
        Return provider capability flags.

        Gitea does not support resolving/unresolving PR review comments.
        """
        return ProviderCapabilities(resolvable_comments=False, supports_suggestions=False)
