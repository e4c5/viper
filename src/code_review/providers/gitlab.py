"""GitLab API provider (merge requests = MR, project id = owner/repo URL-encoded)."""

import logging
from typing import Any
from urllib.parse import quote

import httpx

from code_review.providers.base import (
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    ProviderInterface,
    ReviewComment,
    _log_pr_info_warning,
    pr_info_from_api_dict,
)
from code_review.formatters.comment import render_suggestion_block
from code_review.providers.safety import truncate_repo_content

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
logger = logging.getLogger(__name__)


def _project_id(owner: str, repo: str) -> str:
    """URL-encoded project path for GitLab API."""
    return quote(f"{owner}/{repo}", safe="")


class GitLabProvider(ProviderInterface):
    """GitLab API client for MR diff, file content, and discussion comments."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._token}

    def _path(self, owner: str, repo: str, *parts: str) -> str:
        proj = _project_id(owner, repo)
        return f"{self._base_url}/projects/{proj}/" + "/".join(parts)

    def _get(self, path: str) -> Any:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(path, headers=self._headers())
            r.raise_for_status()
            if r.headers.get("content-type", "").startswith("application/json"):
                return r.json()
            return r.text

    def _get_raw(self, path: str) -> bytes:
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
        """Return unified diff by concatenating MR diffs."""
        path = self._path(owner, repo, "merge_requests", str(pr_number), "diffs")
        data = self._get(path)
        if not isinstance(data, list):
            return ""
        parts: list[str] = []
        for d in data:
            new_path = d.get("new_path") or d.get("old_path") or ""
            old_path = d.get("old_path") or new_path
            diff = d.get("diff") or ""
            if diff:
                parts.append(f"diff --git a/{old_path} b/{new_path}")
                parts.append(diff)
        return "\n".join(parts)

    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Return file content at ref (raw endpoint)."""
        proj = _project_id(owner, repo)
        encoded_path = quote(path, safe="")
        url = f"{self._base_url}/projects/{proj}/repository/files/{encoded_path}/raw"
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(url, headers=self._headers(), params={"ref": ref})
            r.raise_for_status()
            raw = r.content.decode("utf-8", errors="replace")
            return truncate_repo_content(raw, max_bytes=MAX_REPO_FILE_BYTES)

    def _additions_deletions_from_diff(self, d: dict) -> tuple[int, int]:
        """Get (additions, deletions) from API fields or by parsing diff text."""
        add = d.get("additions")
        dele = d.get("deletions")
        if isinstance(add, int) and isinstance(dele, int):
            return add, dele
        new_lines = d.get("new_lines")
        deleted_lines = d.get("deleted_lines")
        if isinstance(new_lines, int) and isinstance(deleted_lines, int):
            return new_lines, deleted_lines
        diff_text = d.get("diff")
        if isinstance(diff_text, str) and diff_text:
            additions = 0
            deletions = 0
            for line in diff_text.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    additions += 1
                elif line.startswith("-") and not line.startswith("---"):
                    deletions += 1
            return additions, deletions
        # Limitation: neither API counts nor diff text available; additions/deletions left zero
        return 0, 0

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        """Return list of changed files from MR diffs."""
        path = self._path(owner, repo, "merge_requests", str(pr_number), "diffs")
        data = self._get(path)
        if not isinstance(data, list):
            return []
        result: list[FileInfo] = []
        for d in data:
            if not isinstance(d, dict):
                continue
            new_path = d.get("new_path") or d.get("old_path") or ""
            if not new_path:
                continue
            if d.get("new_file"):
                status = "added"
            elif d.get("deleted_file"):
                status = "removed"
            else:
                status = "modified"
            additions, deletions = self._additions_deletions_from_diff(d)
            result.append(
                FileInfo(path=new_path, status=status, additions=additions, deletions=deletions)
            )
        return result

    def _get_mr_diff_refs(self, owner: str, repo: str, pr_number: int) -> dict | None:
        """Fetch MR to get diff_refs (base_sha, head_sha, start_sha) for positioning comments."""
        path = self._path(owner, repo, "merge_requests", str(pr_number))
        data = self._get(path)
        if isinstance(data, dict) and "diff_refs" in data:
            return data["diff_refs"]
        return None

    def _render_body(self, comment: InlineComment, with_path_prefix: bool = False) -> str:
        """
        Render comment body, appending suggestion block when suggested_patch is present.
        Optionally prefix with path/line header for MR-level notes.
        """
        base = render_suggestion_block(comment.body, comment.suggested_patch)
        if with_path_prefix:
            return f"**{comment.path}:L{comment.line}**\n\n{base}"
        return base

    def post_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        comments: list[InlineComment],
        head_sha: str = "",
    ) -> None:
        """Post inline comments as MR discussions with position (requires diff_refs from MR)."""
        if not comments:
            return
        diff_refs = self._get_mr_diff_refs(owner, repo, pr_number)
        if not diff_refs:
            # Fallback: post as MR-level note (no line position)
            for c in comments:
                self._post(
                    self._path(owner, repo, "merge_requests", str(pr_number), "notes"),
                    {"body": self._render_body(c, with_path_prefix=True)},
                )
            return
        base_sha = diff_refs.get("base_sha") or ""
        start_sha = diff_refs.get("start_sha") or base_sha
        head_sha_val = head_sha or diff_refs.get("head_sha") or ""
        for c in comments:
            body = self._render_body(c)
            position = {
                "base_sha": base_sha,
                "start_sha": start_sha,
                "head_sha": head_sha_val,
                "position_type": "text",
                "new_path": c.path,
                "old_path": c.path,
                "new_line": c.line,
            }
            self._post(
                self._path(owner, repo, "merge_requests", str(pr_number), "discussions"),
                {"body": body, "position": position},
            )

    def get_existing_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewComment]:
        """Return existing MR discussion notes that are DiffNotes (inline)."""
        path = self._path(owner, repo, "merge_requests", str(pr_number), "discussions")
        data = self._get(path)
        if not isinstance(data, list):
            return []
        result: list[ReviewComment] = []
        for disc in data:
            notes = disc.get("notes") or []
            for n in notes:
                if n.get("type") != "DiffNote":
                    continue
                pos = n.get("position") or {}
                result.append(
                    ReviewComment(
                        id=str(n.get("id", "")),
                        path=pos.get("new_path") or pos.get("old_path") or "",
                        line=int(pos.get("new_line") or pos.get("old_line") or 0),
                        body=n.get("body", ""),
                        resolved=bool(n.get("resolved", False)),
                    )
                )
        return result

    def resolve_comment(self, owner: str, repo: str, comment_id: str) -> None:
        """
        Resolve a discussion thread.

        Not implemented; capabilities() returns resolvable_comments=False so callers
        do not attempt this.
        """
        pass

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post MR-level note (no position)."""
        self._post(
            self._path(owner, repo, "merge_requests", str(pr_number), "notes"),
            {"body": body},
        )

    def get_pr_commit_messages(self, owner: str, repo: str, pr_number: int) -> list[str]:
        """List MR commits (GitLab: GET .../merge_requests/:iid/commits), paginated."""
        base_path = self._path(owner, repo, "merge_requests", str(pr_number), "commits")
        out: list[str] = []
        page = 1
        per_page = 100
        while True:
            path = f"{base_path}?per_page={per_page}&page={page}"
            try:
                data = self._get(path)
            except Exception as e:
                logger.warning(
                    "get_pr_commit_messages failed owner=%s repo=%s pr_number=%s: %s",
                    owner,
                    repo,
                    pr_number,
                    e,
                )
                break
            if not isinstance(data, list):
                break
            for item in data:
                if not isinstance(item, dict):
                    continue
                msg = (item.get("message") or item.get("title") or "").strip()
                if msg:
                    out.append(msg)
            if len(data) < per_page:
                break
            page += 1
        return out

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """Return MR title, labels, and description for skip-review and metadata."""
        try:
            path = self._path(owner, repo, "merge_requests", str(pr_number))
            data = self._get(path)
            return pr_info_from_api_dict(data, "description") if isinstance(data, dict) else None
        except Exception as e:
            _log_pr_info_warning(logger, owner, repo, pr_number, e)
            return None

    def update_pr_description(
        self, owner: str, repo: str, pr_number: int, description: str, title: str | None = None
    ) -> None:
        """Update the MR description (and optionally title) via PUT .../merge_requests/:iid."""
        path = self._path(owner, repo, "merge_requests", str(pr_number))
        payload: dict[str, str] = {"description": description}
        if title is not None:
            payload["title"] = title
        self._put(path, payload)

    def capabilities(self) -> ProviderCapabilities:
        """
        Return provider capability flags for GitLab.

        GitLab supports suggestion blocks. resolve_comment is not implemented, so
        resolvable_comments=False to avoid silent failures.
        """
        return ProviderCapabilities(
            resolvable_comments=False, 
            supports_suggestions=True,
            supports_multiline_suggestions=True,
        )
