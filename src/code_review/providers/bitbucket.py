"""Bitbucket Cloud API provider (workspace = owner, repo_slug = repo)."""

from typing import Any

import httpx

from code_review.diff.parser import parse_unified_diff
from code_review.providers.safety import truncate_repo_content
from code_review.providers.base import (
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    ProviderInterface,
    ReviewComment,
)

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
DEFAULT_BASE_URL = "https://api.bitbucket.org/2.0"


class BitbucketProvider(ProviderInterface):
    """Bitbucket Cloud API client for PR diff, file content, and comments."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str = "", timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    def _path(self, owner: str, repo: str, *parts: str) -> str:
        return f"{self._base_url}/repositories/{owner}/{repo}/" + "/".join(parts)

    def _get(self, path: str) -> Any:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(path, headers=self._headers())
            r.raise_for_status()
            if "application/json" in (r.headers.get("content-type") or ""):
                return r.json()
            return r.text

    def _get_raw_bytes(self, path: str) -> bytes:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(path, headers=self._headers())
            r.raise_for_status()
            return r.content

    def _post(self, path: str, json: dict) -> Any:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(path, headers=self._headers(), json=json)
            r.raise_for_status()
            return r.json() if r.content else None

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff for the PR."""
        path = self._path(owner, repo, "pullrequests", str(pr_number), "diff")
        out = self._get(path)
        return out if isinstance(out, str) else ""

    def get_pr_diff_for_file(
        self, owner: str, repo: str, pr_number: int, file_path: str
    ) -> str:
        """Return diff for a single file by parsing full diff."""
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
        """Return file content at ref (Bitbucket src endpoint)."""
        url = self._path(owner, repo, "src", ref, path)
        raw = self._get_raw_bytes(url)
        text = raw.decode("utf-8", errors="replace")
        return truncate_repo_content(text, max_bytes=MAX_REPO_FILE_BYTES)

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
        """Return list of changed files from PR diffstat."""
        path = self._path(owner, repo, "pullrequests", str(pr_number), "diffstat")
        data = self._get(path)
        if not isinstance(data, dict) or "values" not in data:
            return []
        result: list[FileInfo] = []
        for f in data["values"]:
            # Bitbucket diffstat: new.path or old.path
            file_path = (f.get("new") or {}).get("path") or (f.get("old") or {}).get("path") or ""
            if not file_path:
                continue
            status = "removed" if f.get("status") == "removed" else "added" if f.get("status") == "added" else "modified"
            result.append(FileInfo(path=file_path, status=status, additions=0, deletions=0))
        return result

    def post_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        comments: list[InlineComment],
        head_sha: str = "",
    ) -> None:
        """Post inline comments (Bitbucket Cloud: content.raw + inline.from/to/path)."""
        path = self._path(owner, repo, "pullrequests", str(pr_number), "comments")
        for c in comments:
            payload: dict[str, Any] = {
                "content": {"raw": c.body},
                "inline": {"path": c.path, "from": c.line, "to": c.line},
            }
            self._post(path, payload)

    def get_existing_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewComment]:
        """Return existing PR comments (inline and non-inline)."""
        path = self._path(owner, repo, "pullrequests", str(pr_number), "comments")
        data = self._get(path)
        if not isinstance(data, dict) or "values" not in data:
            return []
        result: list[ReviewComment] = []
        for c in data["values"]:
            inline = c.get("inline") or {}
            path_str = inline.get("path") or ""
            line = int(inline.get("to") or inline.get("from") or 0)
            body = (c.get("content") or {}).get("raw") or ""
            result.append(
                ReviewComment(
                    id=str(c.get("id", "")),
                    path=path_str,
                    line=line,
                    body=body,
                    resolved=False,
                )
            )
        return result

    def post_pr_summary_comment(
        self, owner: str, repo: str, pr_number: int, body: str
    ) -> None:
        """Post PR-level comment (no inline)."""
        path = self._path(owner, repo, "pullrequests", str(pr_number), "comments")
        self._post(path, {"content": {"raw": body}})

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """Return PR title and labels for skip-review check."""
        try:
            path = self._path(owner, repo, "pullrequests", str(pr_number))
            data = self._get(path)
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
        return ProviderCapabilities(resolvable_comments=False, supports_suggestions=False)
