"""Bitbucket Cloud API provider (workspace = owner, repo_slug = repo)."""

import logging
from typing import Any

import httpx

from code_review.formatters.comment import infer_severity_from_comment_body, render_suggestion_block
from code_review.providers.base import (
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    ProviderInterface,
    ReviewComment,
    ReviewDecision,
    UnresolvedReviewItem,
    _log_pr_info_warning,
    normalize_diff_anchor_path,
    pr_info_from_api_dict,
)
from code_review.providers.review_decision_common import effective_review_body
from code_review.providers.safety import truncate_repo_content

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
DEFAULT_BASE_URL = "https://api.bitbucket.org/2.0"
logger = logging.getLogger(__name__)

_BB_PAGINATION_LOOP_MSG = "Bitbucket pagination loop detected (same next URL returned twice): %s"


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

    def _bitbucket_enter_next_link_page(self, url: str, visited: set[str]) -> bool:
        """Mark ``url`` visited for next-link pagination; log and return False on repeat URL."""
        if url in visited:
            logger.warning(_BB_PAGINATION_LOOP_MSG, url)
            return False
        visited.add(url)
        return True

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

    def _delete(self, path: str) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.delete(path, headers=self._headers())
            r.raise_for_status()

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
        visited: set[str] = set()
        while url:
            if not self._bitbucket_enter_next_link_page(url, visited):
                break
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
        return (entry.get("new") or {}).get("path") or (entry.get("old") or {}).get("path") or ""

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
        visited: set[str] = set()
        while url:
            if not self._bitbucket_enter_next_link_page(url, visited):
                break
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

    @staticmethod
    def _bbcloud_open_task_from_value(t: Any, out_len: int) -> UnresolvedReviewItem | None:
        if not isinstance(t, dict):
            return None
        state = (str(t.get("state") or "")).strip().upper()
        if state in ("RESOLVED", "DECLINED", "CLOSED", "FULFILLED"):
            return None
        content = t.get("content")
        raw_src = content.get("raw") if isinstance(content, dict) else ""
        raw = str(raw_src or "").strip()
        if not raw:
            return None
        tid = str(t.get("id", "") or "")
        return UnresolvedReviewItem(
            stable_id=f"bbcloud:task:{tid}" if tid else f"bbcloud:task:{out_len}",
            thread_id=tid or None,
            kind="task",
            path="",
            line=0,
            body=raw,
            inferred_severity=infer_severity_from_comment_body(raw),
        )

    @staticmethod
    def _bbcloud_append_open_tasks_from_page(
        values: list[Any], out: list[UnresolvedReviewItem]
    ) -> None:
        for t in values:
            item = BitbucketProvider._bbcloud_open_task_from_value(t, len(out))
            if item is not None:
                out.append(item)

    def get_unresolved_review_items_for_quality_gate(
        self, owner: str, repo: str, pr_number: int
    ) -> list[UnresolvedReviewItem]:
        """Bitbucket Cloud: only open PR tasks expose resolved state; comments do not."""
        url: str | None = self._path(owner, repo, "pullrequests", str(pr_number), "tasks")
        out: list[UnresolvedReviewItem] = []
        visited: set[str] = set()
        while url:
            if not self._bitbucket_enter_next_link_page(url, visited):
                break
            try:
                data = self._get(url)
            except Exception as e:
                logger.warning(
                    "Bitbucket Cloud PR tasks fetch failed owner=%s repo=%s pr=%s: %s",
                    owner,
                    repo,
                    pr_number,
                    e,
                )
                break
            if not isinstance(data, dict):
                break
            values = data.get("values")
            if not isinstance(values, list):
                break
            self._bbcloud_append_open_tasks_from_page(values, out)
            nxt = data.get("next")
            url = nxt.strip() if isinstance(nxt, str) and nxt.strip() else None
        return out

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post PR-level comment (no inline)."""
        path = self._path(owner, repo, "pullrequests", str(pr_number), "comments")
        self._post(path, {"content": {"raw": body}})

    def _clear_participant_state(self, path: str, label: str, pr_number: int) -> None:
        """DELETE an approve or request-changes endpoint; 404 means already clear (safe).

        Used to clean up the prior participant state before writing a new one when the
        agent re-runs on an updated PR and the decision flips direction.
        """
        try:
            self._delete(path)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code == 404:
                return  # Not in that state - nothing to clear
            logger.warning(
                "Bitbucket Cloud clear %s failed (HTTP %s) for PR %s",
                label,
                code,
                pr_number,
            )
        except Exception as exc:
            logger.warning(
                "Bitbucket Cloud clear %s failed for PR %s: %s",
                label,
                pr_number,
                exc,
            )

    def submit_review_decision(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        decision: ReviewDecision,
        *,
        body: str = "",
        head_sha: str = "",
    ) -> None:
        """Approve or request changes (Bitbucket Cloud 2.0).

        ``POST {base}/approve`` and ``POST {base}/request-changes`` do not accept a review
        rationale in the JSON body. We post the runner summary to
        ``pullrequests/<id>/comments`` (:meth:`post_pr_summary_comment`) using
        ``effective_review_body(body)`` (see ``review_decision_common``).
        ``head_sha`` is unused.

        Before writing the new state the opposite endpoint is cleared first
        (``DELETE /request-changes`` before approving; ``DELETE /approve`` before requesting
        changes) so that a re-run on an updated PR cannot leave the bot simultaneously in both
        states.  A 404 on the DELETE is silently ignored (already clear).
        """
        _ = head_sha
        base = self._path(owner, repo, "pullrequests", str(pr_number))
        if decision == "APPROVE":
            self._clear_participant_state(f"{base}/request-changes", "request-changes", pr_number)
            self._post(f"{base}/approve", {})
        else:
            self._clear_participant_state(f"{base}/approve", "approve", pr_number)
            self._post(f"{base}/request-changes", {})
        self.post_pr_summary_comment(owner, repo, pr_number, effective_review_body(body))

    def get_pr_commit_messages(self, owner: str, repo: str, pr_number: int) -> list[str]:
        """List commits on the PR (paginated)."""
        url: str | None = self._path(owner, repo, "pullrequests", str(pr_number), "commits")
        out: list[str] = []
        visited: set[str] = set()
        while url:
            if not self._bitbucket_enter_next_link_page(url, visited):
                break
            data = self._safe_get_commit_page(url, owner, repo, pr_number)
            if data is None:
                return out
            out.extend(self._messages_from_commit_page(data))
            url = self._next_page_url(data)
        return out

    def _safe_get_commit_page(
        self,
        url: str,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> dict[str, Any] | None:
        try:
            data = self._get(url)
        except Exception as e:
            logger.warning(
                "get_pr_commit_messages failed owner=%s repo=%s pr_number=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return None
        if not isinstance(data, dict):
            return None
        return data

    @staticmethod
    def _messages_from_commit_page(data: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for item in data.get("values") or []:
            if not isinstance(item, dict):
                continue
            raw_m = item.get("message")
            msg = (raw_m.get("raw") if isinstance(raw_m, dict) else raw_m) or ""
            msg = str(msg).strip()
            if msg:
                out.append(msg)
        return out

    @staticmethod
    def _next_page_url(data: dict[str, Any]) -> str | None:
        nxt = data.get("next")
        return nxt.strip() if isinstance(nxt, str) and nxt.strip() else None

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
            supports_multiline_suggestions=True,
            markup_hides_html_comment=False,
            markup_supports_collapsible=False,
            omit_fingerprint_marker_in_body=True,
            supports_review_decisions=True,
        )
