"""Bitbucket Cloud API provider (workspace = owner, repo_slug = repo)."""

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
    normalize_diff_anchor_path,
    pr_info_from_api_dict,
)
from code_review.formatters.comment import render_suggestion_block
from code_review.providers.safety import truncate_repo_content

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
DEFAULT_BASE_URL = "https://api.bitbucket.org/2.0"
logger = logging.getLogger(__name__)


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

    def _put(self, path: str, json: dict) -> Any:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.put(path, headers=self._headers(), json=json)
            r.raise_for_status()
            return r.json() if r.content else None

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff for the PR."""
        path = self._path(owner, repo, "pullrequests", str(pr_number), "diff")
        out = self._get(path)
        return out if isinstance(out, str) else ""

    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Return file content at ref (Bitbucket src endpoint)."""
        url = self._path(owner, repo, "src", ref, path)
        raw = self._get_raw_bytes(url)
        text = raw.decode("utf-8", errors="replace")
        return truncate_repo_content(text, max_bytes=MAX_REPO_FILE_BYTES)

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        """Return list of changed files from PR diffstat (paginated)."""
        url: str | None = self._path(owner, repo, "pullrequests", str(pr_number), "diffstat")
        result: list[FileInfo] = []
        while url:
            data = self._get(url)
            page_files, next_url = self._parse_diffstat_page(data)
            result.extend(page_files)
            if not next_url:
                break
            url = next_url
        return result

    def _parse_diffstat_page(self, data: Any) -> tuple[list[FileInfo], str | None]:
        """Parse one diffstat page into FileInfo objects and return (files, next_url)."""
        if not isinstance(data, dict):
            return [], None
        values = data.get("values")
        if not isinstance(values, list):
            return [], None

        files: list[FileInfo] = []
        for entry in values:
            if not isinstance(entry, dict):
                continue
            file_path = self._file_path_from_diffstat_entry(entry)
            if not file_path:
                continue
            status = self._status_from_diffstat(entry.get("status"))
            files.append(FileInfo(path=file_path, status=status, additions=0, deletions=0))

        next_url = data.get("next")
        if not next_url or not isinstance(next_url, str):
            return files, None
        stripped = next_url.strip()
        return files, stripped or None

    @staticmethod
    def _file_path_from_diffstat_entry(entry: dict[str, Any]) -> str:
        return ((entry.get("new") or {}).get("path") or (entry.get("old") or {}).get("path") or "")

    @staticmethod
    def _status_from_diffstat(raw_status: Any) -> str:
        status_map = {
            "removed": "removed",
            "added": "added",
        }
        return status_map.get(raw_status, "modified")

    def _anchor_path_for_diff(self, file_path: str) -> str:
        """Normalize path so it matches the PR diff (enables inline comments on the diff view)."""
        return normalize_diff_anchor_path(file_path)

    def post_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        comments: list[InlineComment],
        head_sha: str = "",
    ) -> None:
        """Post inline comments (Bitbucket Cloud: content.raw + inline.from/to/path).
        Path is normalized so it matches the diff and comments appear on the file diff view.

        For single-line comments `from` is omitted (null) per the Bitbucket Cloud API
        spec — setting `from` equal to `to` is treated as a zero-length range and may
        cause the API to reject the comment or display it outside the diff view.
        `from` is only set for genuine multi-line range comments (end_line != line).
        """
        path = self._path(owner, repo, "pullrequests", str(pr_number), "comments")
        for c in comments:
            anchor_path = self._anchor_path_for_diff(c.path)
            end = c.end_line if c.end_line is not None else c.line
            inline: dict[str, Any] = {
                "path": anchor_path,
                "to": end,
            }
            # Only set 'from' for genuine multi-line range comments.
            # For single-line comments, omitting 'from' (null) is the correct API form.
            if c.end_line is not None and c.end_line != c.line:
                inline["from"] = c.line
            payload: dict[str, Any] = {
                "content": {"raw": render_suggestion_block(c.body, c.suggested_patch)},
                "inline": inline,
            }
            self._post(path, payload)

    def get_existing_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewComment]:
        """Return existing PR comments (inline and non-inline; paginated)."""
        url: str | None = self._path(owner, repo, "pullrequests", str(pr_number), "comments")
        result: list[ReviewComment] = []
        while url:
            data = self._get(url)
            page_comments, next_url = self._comments_from_page(data)
            result.extend(page_comments)
            if not next_url:
                break
            url = next_url
        return result

    def _comments_from_page(self, data: Any) -> tuple[list[ReviewComment], str | None]:
        """Parse one comments page. Returns (comments, next_url)."""
        if not isinstance(data, dict):
            return [], None
        values = data.get("values")
        if not isinstance(values, list):
            return [], None

        comments: list[ReviewComment] = []
        for c in values:
            if not isinstance(c, dict):
                continue
            inline = c.get("inline") or {}
            path_str = inline.get("path") or ""
            line = int(inline.get("to") or inline.get("from") or 0)
            body = (c.get("content") or {}).get("raw") or ""
            comments.append(
                ReviewComment(
                    id=str(c.get("id", "")),
                    path=path_str,
                    line=line,
                    body=body,
                    resolved=False,
                )
            )

        next_url = data.get("next")
        if not next_url or not isinstance(next_url, str):
            return comments, None
        stripped = next_url.strip()
        return comments, stripped or None

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post PR-level comment (no inline)."""
        path = self._path(owner, repo, "pullrequests", str(pr_number), "comments")
        self._post(path, {"content": {"raw": body}})

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """Return PR title, labels, and description for skip-review and metadata.
        Bitbucket Cloud REST API v2.0 does not support PR labels; labels will always be empty.
        """
        try:
            path = self._path(owner, repo, "pullrequests", str(pr_number))
            data = self._get(path)
            return pr_info_from_api_dict(data, "description") if isinstance(data, dict) else None
        except Exception as e:
            _log_pr_info_warning(logger, owner, repo, pr_number, e)
            return None

    def update_pr_description(
        self, owner: str, repo: str, pr_number: int, description: str, title: str | None = None
    ) -> None:
        """Update the PR description via PUT .../pullrequests/:id (description.raw for body)."""
        path = self._path(owner, repo, "pullrequests", str(pr_number))
        payload: dict = {"description": {"raw": description}}
        if title is not None:
            payload["title"] = title
        self._put(path, payload)

    def capabilities(self) -> ProviderCapabilities:
        # PR labels are not supported by Bitbucket Cloud API.
        # Skip-by-label is ineffective; see get_pr_info.
        return ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=True,
            markup_hides_html_comment=False,
            markup_supports_collapsible=False,
            omit_fingerprint_marker_in_body=True,
        )
