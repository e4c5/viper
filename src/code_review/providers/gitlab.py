"""GitLab API provider (merge requests = MR, project id = owner/repo URL-encoded)."""

import logging
from typing import Any, Literal
from urllib.parse import quote

import httpx

from code_review.formatters.comment import (
    infer_severity_from_comment_body,
    max_inferred_severity,
    render_suggestion_block,
)
from code_review.providers.base import (
    BotAttributionIdentity,
    BotBlockingState,
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    ProviderInterface,
    ReviewComment,
    ReviewDecision,
    UnresolvedReviewItem,
    _log_pr_info_warning,
    pr_info_from_api_dict,
)
from code_review.providers.http_shortcuts import (
    http_delete,
    http_get_json_or_text,
    http_post_json,
    http_put_json,
)
from code_review.providers.review_decision_common import (
    delete_soft_fail,
    gitlab_note_with_submit_review_requested_changes,
)
from code_review.providers.safety import truncate_repo_content
from code_review.schemas.review_thread_dismissal import (
    ReviewThreadDismissalContext,
    ReviewThreadDismissalEntry,
)

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
logger = logging.getLogger(__name__)


def _project_id(owner: str, repo: str) -> str:
    """URL-encoded project path for GitLab API."""
    return quote(f"{owner}/{repo}", safe="")


def _gitlab_user_id_or_none(me: Any) -> int | None:
    if not isinstance(me, dict) or me.get("id") is None:
        return None
    return int(me["id"])


def _gitlab_mine_mr_reviews_for_user(data: list[Any], my_id: int) -> list[tuple[int, str]]:
    mine: list[tuple[int, str]] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        u = r.get("user") or {}
        if not isinstance(u, dict) or u.get("id") is None:
            continue
        if int(u["id"]) != my_id:
            continue
        mine.append((int(r.get("id") or 0), str(r.get("state") or "").strip().lower()))
    return mine


def _gitlab_bot_blocking_from_sorted_mine(mine: list[tuple[int, str]]) -> BotBlockingState:
    if not mine:
        return "NOT_BLOCKING"
    mine.sort(key=lambda x: x[0])
    last = mine[-1][1]
    if last == "requested_changes":
        return "BLOCKING"
    if last == "approved":
        return "NOT_BLOCKING"
    return "UNKNOWN"


def _gitlab_notes_contain_id(notes: Any, want: str) -> bool:
    if not isinstance(notes, list):
        return False
    for n in notes:
        if isinstance(n, dict) and str(n.get("id") or "") == want:
            return True
    return False


def _gitlab_dismissal_entries_from_notes(notes: list[Any]) -> list[ReviewThreadDismissalEntry]:
    entries: list[ReviewThreadDismissalEntry] = []
    for n in notes:
        if not isinstance(n, dict):
            continue
        author = n.get("author") if isinstance(n.get("author"), dict) else {}
        uname = str(author.get("username") or author.get("name") or "")
        entries.append(
            ReviewThreadDismissalEntry(
                comment_id=str(n.get("id") or ""),
                author_login=uname,
                body=str(n.get("body") or ""),
                created_at=str(n.get("created_at") or ""),
            )
        )
    return entries


def _gitlab_dismissal_context_for_discussion(
    disc: dict[str, Any], want: str, pr_number: int
) -> ReviewThreadDismissalContext | None:
    did = str(disc.get("id") or "")
    notes = disc.get("notes") or []
    if not isinstance(notes, list):
        return None
    if not _gitlab_notes_contain_id(notes, want):
        return None
    entries = _gitlab_dismissal_entries_from_notes(notes)
    if len(entries) < 2:
        return None
    stable = f"gitlab:discussion:{did}" if did else f"gitlab:discussion:{pr_number}"
    return ReviewThreadDismissalContext(
        gate_exclusion_stable_id=stable,
        entries=entries,
    )


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

    def _get_mr_discussions_paginated(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """List all MR discussions (GitLab paginates; small pages omit later threads)."""
        base_path = self._path(owner, repo, "merge_requests", str(pr_number), "discussions")
        combined: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        max_pages = 500
        for _ in range(max_pages):
            url = f"{base_path}?per_page={per_page}&page={page}"
            try:
                data = self._get(url)
            except Exception as e:
                logger.warning(
                    "GitLab MR discussions fetch failed owner=%s repo=%s pr_number=%s page=%s: %s",
                    owner,
                    repo,
                    pr_number,
                    page,
                    e,
                )
                break
            if not isinstance(data, list):
                break
            if not data:
                break
            for item in data:
                if isinstance(item, dict):
                    combined.append(item)
            if len(data) < per_page:
                break
            page += 1
        return combined

    def _get_mr_reviews_paginated(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]] | None:
        """List all MR reviews (paginated). ``None`` if any page fails or response is not a list."""
        base_path = self._path(owner, repo, "merge_requests", str(pr_number), "reviews")
        combined: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        max_pages = 500
        for _ in range(max_pages):
            url = f"{base_path}?per_page={per_page}&page={page}"
            try:
                data = self._get(url)
            except Exception as e:
                logger.warning(
                    "GitLab MR reviews fetch failed owner=%s repo=%s pr_number=%s page=%s: %s",
                    owner,
                    repo,
                    pr_number,
                    page,
                    e,
                )
                return None
            if not isinstance(data, list):
                return None
            for item in data:
                if isinstance(item, dict):
                    combined.append(item)
            if len(data) < per_page:
                break
            page += 1
        return combined

    def _get(self, path: str) -> Any:
        return http_get_json_or_text(path, headers=self._headers(), timeout=self._timeout)

    def _post(self, path: str, json: dict) -> Any:
        return http_post_json(path, json, headers=self._headers(), timeout=self._timeout)

    def _put(self, path: str, json: dict) -> Any:
        return http_put_json(path, json, headers=self._headers(), timeout=self._timeout)

    def _delete(self, path: str) -> None:
        http_delete(path, headers=self._headers(), timeout=self._timeout)

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff by concatenating MR diffs."""
        path = self._path(owner, repo, "merge_requests", str(pr_number), "diffs")
        data = self._get(path)
        return self._unified_diff_from_diff_entries(data)

    @staticmethod
    def _unified_diff_from_diff_entries(data: Any) -> str:
        """Build unified diff text from GitLab diff-entry payloads."""
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

    def get_incremental_pr_diff(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> str:
        """Return unified diff for the incremental compare range ``base_sha..head_sha``."""
        if not base_sha or not head_sha or base_sha == head_sha:
            return self.get_pr_diff(owner, repo, pr_number)
        path = self._path(owner, repo, "repository", "compare")
        data = self._get(f"{path}?from={quote(base_sha, safe='')}&to={quote(head_sha, safe='')}")
        if not isinstance(data, dict):
            return ""
        return self._unified_diff_from_diff_entries(data.get("diffs"))

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
        return self._file_infos_from_diff_entries(data)

    def _file_infos_from_diff_entries(self, data: Any) -> list[FileInfo]:
        """Return FileInfo objects from GitLab MR/compare diff entries."""
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

    def get_incremental_pr_files(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> list[FileInfo]:
        """Return files changed in the incremental compare range ``base_sha..head_sha``."""
        if not base_sha or not head_sha or base_sha == head_sha:
            return self.get_pr_files(owner, repo, pr_number)
        path = self._path(owner, repo, "repository", "compare")
        data = self._get(f"{path}?from={quote(base_sha, safe='')}&to={quote(head_sha, safe='')}")
        if not isinstance(data, dict):
            return []
        return self._file_infos_from_diff_entries(data.get("diffs"))

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
        data = self._get_mr_discussions_paginated(owner, repo, pr_number)
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

    @staticmethod
    def _gitlab_diff_notes_from_discussion(notes: Any) -> list[dict[str, Any]]:
        if not isinstance(notes, list):
            return []
        return [n for n in notes if isinstance(n, dict) and n.get("type") == "DiffNote"]

    @staticmethod
    def _gitlab_unresolved_item_from_diff_notes(
        did: str,
        diff_notes: list[dict[str, Any]],
        out_len: int,
    ) -> UnresolvedReviewItem | None:
        best_sev: Literal["high", "medium", "low", "nit", "unknown"] = "unknown"
        body_text = ""
        path_str = ""
        line_no = 0
        for n in diff_notes:
            raw = (n.get("body") or "").strip()
            if not raw:
                continue
            sev = infer_severity_from_comment_body(n.get("body") or "")
            best_sev = max_inferred_severity(best_sev, sev)
            if not body_text:
                body_text = n.get("body") or ""
                pos = n.get("position") or {}
                path_str = str(pos.get("new_path") or pos.get("old_path") or "")
                line_no = int(pos.get("new_line") or pos.get("old_line") or 0)
        if not body_text:
            return None
        return UnresolvedReviewItem(
            stable_id=f"gitlab:discussion:{did}" if did else f"gitlab:discussion:{out_len}",
            thread_id=did or None,
            kind="discussion_thread",
            path=path_str,
            line=line_no,
            body=body_text,
            inferred_severity=best_sev,
        )

    def get_unresolved_review_items_for_quality_gate(
        self, owner: str, repo: str, pr_number: int
    ) -> list[UnresolvedReviewItem]:
        """One item per unresolved MR discussion (thread), severity = max across DiffNotes."""
        data = self._get_mr_discussions_paginated(owner, repo, pr_number)
        out: list[UnresolvedReviewItem] = []
        for disc in data:
            if not isinstance(disc, dict) or disc.get("resolved"):
                continue
            did = str(disc.get("id", "") or "")
            diff_notes = self._gitlab_diff_notes_from_discussion(disc.get("notes"))
            if not diff_notes:
                continue
            item = self._gitlab_unresolved_item_from_diff_notes(did, diff_notes, len(out))
            if item is not None:
                out.append(item)
        return out

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

    def get_bot_blocking_state(self, owner: str, repo: str, pr_number: int) -> BotBlockingState:
        """Use MR reviews API when available; ``requested_changes`` → blocking."""
        try:
            me = self._get(f"{self._base_url}/user")
            my_id = _gitlab_user_id_or_none(me)
            if my_id is None:
                return "UNKNOWN"
            data = self._get_mr_reviews_paginated(owner, repo, pr_number)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code == 404:
                return "UNKNOWN"
            logger.warning(
                "GitLab get_bot_blocking_state HTTP error owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                exc,
            )
            return "UNKNOWN"
        except Exception as e:
            logger.warning(
                "GitLab get_bot_blocking_state failed owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return "UNKNOWN"
        if data is None:
            return "UNKNOWN"
        mine = _gitlab_mine_mr_reviews_for_user(data, my_id)
        return _gitlab_bot_blocking_from_sorted_mine(mine)

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
        """Submit MR approval or request-changes via GitLab REST + quick actions.

        * ``APPROVE`` → ``POST .../merge_requests/:iid/approve`` (optional ``sha``).
        * ``REQUEST_CHANGES`` → first removes any prior approval via ``DELETE .../approve``
          (so the bot cannot be simultaneously approved and requesting changes after a PR is
          updated), then posts an MR note with ``/submit_review requested_changes`` (requires a
          pending review in some GitLab versions; see GitLab merge request reviews docs).
        """
        base = self._path(owner, repo, "merge_requests", str(pr_number))
        if decision == "APPROVE":
            payload: dict[str, str] = {}
            if head_sha:
                payload["sha"] = head_sha
            self._post(f"{base}/approve", payload)
            return
        # Remove any prior bot approval before requesting changes so the MR is not left
        # in the contradictory "approved + request changes" state when the PR is re-reviewed.
        delete_soft_fail(
            self._delete,
            f"{base}/approve",
            safe_codes=frozenset({403, 404, 405}),
            log_label=f"GitLab unapprove owner={owner} repo={repo} pr={pr_number}",
        )
        note = gitlab_note_with_submit_review_requested_changes(body)
        self._post(f"{base}/notes", {"body": note})

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
            out.extend(
                msg
                for item in data
                if isinstance(item, dict)
                for msg in [(item.get("message") or item.get("title") or "").strip()]
                if msg
            )
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

    def get_bot_attribution_identity(
        self, owner: str, repo: str, pr_number: int
    ) -> BotAttributionIdentity:
        try:
            data = self._get(f"{self._base_url}/user")
            if isinstance(data, dict):
                login = str(data.get("username") or "").strip().lower()
                uid = str(data.get("id") or "").strip()
                return BotAttributionIdentity(login=login, id_str=uid)
        except Exception as e:
            logger.warning("GitLab get_bot_attribution_identity failed: %s", e)
        return BotAttributionIdentity()

    def _gitlab_discussion_id_for_note_id(
        self, owner: str, repo: str, pr_number: int, note_id: str
    ) -> str:
        want = (note_id or "").strip()
        if not want:
            return ""
        try:
            data = self._get_mr_discussions_paginated(owner, repo, pr_number)
        except Exception as e:
            logger.warning(
                "GitLab discussion lookup failed owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return ""
        for disc in data:
            if not isinstance(disc, dict):
                continue
            did = str(disc.get("id") or "")
            if _gitlab_notes_contain_id(disc.get("notes") or [], want):
                return did
        return ""

    def get_review_thread_dismissal_context(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        triggered_comment_id: str,
    ) -> ReviewThreadDismissalContext | None:
        want = (triggered_comment_id or "").strip()
        if not want:
            return None
        try:
            data = self._get_mr_discussions_paginated(owner, repo, pr_number)
        except Exception as e:
            logger.warning(
                "GitLab get_review_thread_dismissal_context failed owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return None
        for disc in data:
            if not isinstance(disc, dict):
                continue
            ctx = _gitlab_dismissal_context_for_discussion(disc, want, pr_number)
            if ctx is not None:
                return ctx
        return None

    def post_review_thread_reply(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        reply_to_comment_id: str,
        body: str,
    ) -> None:
        disc_id = self._gitlab_discussion_id_for_note_id(
            owner, repo, pr_number, reply_to_comment_id
        )
        if not disc_id:
            raise ValueError(f"No GitLab discussion for note id {reply_to_comment_id!r}")
        path = self._path(
            owner, repo, "merge_requests", str(pr_number), "discussions", disc_id, "notes"
        )
        self._post(path, {"body": body})

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
            supports_review_decisions=True,
            supports_bot_blocking_state_query=True,
            supports_bot_attribution_identity_query=True,
            supports_review_thread_dismissal_context=True,
            supports_review_thread_reply=True,
        )
