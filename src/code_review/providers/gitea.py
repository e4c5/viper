"""Gitea API provider."""

import time
from typing import Any

import httpx

from code_review.diff.parser import parse_unified_diff
from code_review.providers.safety import truncate_repo_content

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
from code_review.providers.base import (
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    ProviderInterface,
    ReviewComment,
)


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

    _RETRY_STATUSES = (429, 502, 503, 504)
    _RETRY_DELAY_SECONDS = 1.0

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        max_retries: int = 1,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform request with one retry on 429/5xx."""
        with httpx.Client(timeout=self._timeout) as client:
            r = client.request(method, url, headers=self._headers(), **kwargs)
            if r.status_code in self._RETRY_STATUSES and max_retries > 0:
                time.sleep(self._RETRY_DELAY_SECONDS)
                r = client.request(method, url, headers=self._headers(), **kwargs)
            return r

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._base_url}/api/v1{path}"
        r = self._request_with_retry("GET", url, params=params)
        r.raise_for_status()
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text

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

    def get_pr_diff_for_file(
        self, owner: str, repo: str, pr_number: int, file_path: str
    ) -> str:
        """Return diff for a single file. Parses full diff and slices by file."""
        full_diff = self.get_pr_diff(owner, repo, pr_number)
        hunks = parse_unified_diff(full_diff)
        lines: list[str] = []
        headers_emitted = False
        for hunk in hunks:
            if hunk.path != file_path:
                continue
            if not headers_emitted:
                lines.append(f"--- a/{hunk.path}")
                lines.append(f"+++ b/{hunk.path}")
                headers_emitted = True
            lines.append(
                f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@"
            )
            for content, old_ln, new_ln in hunk.lines:
                if old_ln is not None and new_ln is not None:
                    lines.append(" " + content)
                elif new_ln is not None:
                    lines.append("+" + content)
                elif old_ln is not None:
                    lines.append("-" + content)
                else:
                    lines.append("\\" + content)
        return "\n".join(lines) if lines else ""

    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Return file content at ref; truncated with delimiter if over max size."""
        api_path = f"/repos/{owner}/{repo}/contents/{path}"
        resp = self._get(api_path, params={"ref": ref})
        if isinstance(resp, dict) and "content" in resp:
            import base64
            raw = base64.b64decode(resp["content"]).decode("utf-8", errors="replace")
            return truncate_repo_content(raw, max_bytes=MAX_REPO_FILE_BYTES)
        raise ValueError(f"Unexpected response for {path} at {ref}")

    def get_file_lines(
        self,
        owner: str,
        repo: str,
        ref: str,
        path: str,
        start_line: int,
        end_line: int,
    ) -> str:
        """Return lines start_line..end_line (1-based inclusive) from file at ref."""
        content = self.get_file_content(owner, repo, ref, path)
        lines = content.splitlines()
        if start_line < 1 or end_line < start_line:
            return ""
        start_idx = start_line - 1
        end_idx = min(end_line, len(lines))
        return "\n".join(lines[start_idx:end_idx])

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        """Return list of changed files in the PR."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
        data = self._get(path)
        if not isinstance(data, list):
            return []
        result: list[FileInfo] = []
        for f in data:
            status = f.get("status", "modified")
            result.append(
                FileInfo(
                    path=f.get("filename", f.get("path", "")),
                    status=status,
                    additions=f.get("additions", 0),
                    deletions=f.get("deletions", 0),
                )
            )
        return result

    def post_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        comments: list[InlineComment],
        head_sha: str = "",
    ) -> None:
        """Post inline review comments. Convert internal InlineComment to Gitea API payload."""
        if not comments:
            return
        # Gitea CreatePullReview: body, event (APPROVE/REQUEST_CHANGES/COMMENT), comments
        # Each comment: path, body, line (1-based); end_line not in Gitea API
        review_comments = [
            {"path": c.path, "body": c.body, "line": c.line}
            for c in comments
        ]
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

    def post_pr_summary_comment(
        self, owner: str, repo: str, pr_number: int, body: str
    ) -> None:
        """Post PR-level comment. In Gitea, PRs are issues; use issues comments endpoint."""
        self._post(
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            {"body": body},
        )

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """Return PR title and labels for skip-review check."""
        try:
            data = self._get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
            if not isinstance(data, dict):
                return None
            title = data.get("title", "") or ""
            labels_raw = data.get("labels") or []
            labels = [
                lb.get("name", lb) if isinstance(lb, dict) else str(lb)
                for lb in labels_raw
            ]
            return PRInfo(title=title, labels=labels)
        except Exception:
            return None

    def capabilities(self) -> ProviderCapabilities:
        """Return provider capability flags. Gitea does not support resolving/unresolving PR review comments."""
        return ProviderCapabilities(resolvable_comments=False, supports_suggestions=False)
