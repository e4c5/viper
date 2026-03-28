"""Bitbucket Cloud API provider (workspace = owner, repo_slug = repo)."""

import logging
from datetime import datetime
from typing import Any

from code_review.formatters.comment import infer_severity_from_comment_body, render_suggestion_block
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
    default_unresolved_review_items_from_comments,
    normalize_diff_anchor_path,
    pr_info_from_api_dict,
)
from code_review.providers.http_shortcuts import (
    http_delete,
    http_get_bytes,
    http_get_json_or_text,
    http_post_json,
    http_put_json,
)
from code_review.providers.review_decision_common import delete_soft_fail, effective_review_body
from code_review.providers.safety import truncate_repo_content
from code_review.reply_dismissal_state import is_reply_dismissal_accepted_reply
from code_review.schemas.review_thread_dismissal import (
    ReviewThreadDismissalContext,
    ReviewThreadDismissalEntry,
)

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
DEFAULT_BASE_URL = "https://api.bitbucket.org/2.0"
logger = logging.getLogger(__name__)

_BB_PAGINATION_LOOP_MSG = "Bitbucket pagination loop detected (same next URL returned twice): %s"


def _bitbucket_cloud_blocking_from_participants(
    participants: list[Any], my_uuid: str
) -> BotBlockingState:
    for p in participants:
        if not isinstance(p, dict):
            continue
        user = p.get("user") or {}
        if not isinstance(user, dict):
            continue
        uid = str(user.get("uuid") or "").strip()
        if uid != my_uuid:
            continue
        st = str(p.get("state") or "").strip().lower().replace(" ", "_")
        if st in ("changes_requested", "needs_work"):
            return "BLOCKING"
        if p.get("approved") is True or st == "approved":
            return "NOT_BLOCKING"
        return "UNKNOWN"
    return "NOT_BLOCKING"


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
        return http_get_json_or_text(path, headers=self._headers(), timeout=self._timeout)

    def _get_raw_bytes(self, path: str) -> bytes:
        return http_get_bytes(path, headers=self._headers(), timeout=self._timeout)

    def _post(self, path: str, json: dict) -> Any:
        return http_post_json(path, json, headers=self._headers(), timeout=self._timeout)

    def _put(self, path: str, json: dict) -> Any:
        return http_put_json(path, json, headers=self._headers(), timeout=self._timeout)

    def _delete(self, path: str) -> None:
        http_delete(path, headers=self._headers(), timeout=self._timeout)

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff for the PR."""
        path = self._path(owner, repo, "pullrequests", str(pr_number), "diff")
        out = self._get(path)
        return out if isinstance(out, str) else ""

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
        spec = f"{base_sha}..{head_sha}"
        out = self._get(self._path(owner, repo, "diff", spec))
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
        return self._get_diffstat_files(url)

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
        spec = f"{base_sha}..{head_sha}"
        return self._get_diffstat_files(self._path(owner, repo, "diffstat", spec))

    def _get_diffstat_files(self, url: str | None) -> list[FileInfo]:
        """Return FileInfo objects from a Bitbucket Cloud diffstat URL."""
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

        comments = [
            self._bbcloud_review_comment_from_api_dict(comment)
            for comment in values
            if isinstance(comment, dict)
        ]
        return comments, self._next_page_url(data)

    def _bbcloud_review_comment_from_api_dict(self, comment: dict[str, Any]) -> ReviewComment:
        inline = comment.get("inline") if isinstance(comment.get("inline"), dict) else {}
        return ReviewComment(
            id=str(comment.get("id", "")),
            path=str(inline.get("path") or ""),
            line=self._bbcloud_inline_line(inline),
            body=self._bbcloud_comment_body(comment),
            resolved=False,
            outdated=bool(inline.get("outdated") is True),
            parent_id=self._bbcloud_parent_id(comment),
            author_login=self._bbcloud_comment_author_login(comment),
            created_at=str(comment.get("created_on") or ""),
        )

    @staticmethod
    def _bbcloud_inline_line(inline: dict[str, Any]) -> int:
        return int(inline.get("to") or inline.get("from") or 0)

    @staticmethod
    def _bbcloud_comment_body(comment: dict[str, Any]) -> str:
        content = comment.get("content")
        if not isinstance(content, dict):
            return ""
        return str(content.get("raw") or "")

    @staticmethod
    def _bbcloud_comment_author_login(comment: dict[str, Any]) -> str:
        user = comment.get("user")
        if not isinstance(user, dict):
            return ""
        return str(user.get("nickname") or user.get("username") or user.get("display_name") or "")

    @staticmethod
    def _bbcloud_is_inline_root_unresolved_comment(comment: ReviewComment) -> bool:
        if comment.resolved:
            return False
        if comment.outdated:
            return False
        if comment.parent_id:
            return False
        return bool(comment.path or int(comment.line or 0) > 0)

    @staticmethod
    def _bbcloud_comment_is_bot_authored(comment: ReviewComment, bot: BotAttributionIdentity) -> bool:
        author_login = (comment.author_login or "").strip().lower()
        if not author_login:
            return False
        bot_login = (bot.login or "").strip().lower()
        if bot_login and author_login == bot_login:
            return True
        bot_uuid = (bot.uuid or "").strip().lower().replace("{", "").replace("}", "")
        return bool(bot_uuid and author_login.replace("{", "").replace("}", "") == bot_uuid)

    @staticmethod
    def _bbcloud_thread_root_comment_id(
        by_id: dict[str, ReviewComment],
        comment_id: str,
    ) -> str:
        root = (comment_id or "").strip()
        seen: set[str] = set()
        while root in by_id:
            parent_id = (by_id[root].parent_id or "").strip()
            if not parent_id or parent_id not in by_id or root in seen:
                break
            seen.add(root)
            root = parent_id
        return root

    def _bbcloud_persisted_dismissed_root_ids(
        self,
        comments: list[ReviewComment],
        bot: BotAttributionIdentity,
    ) -> frozenset[str]:
        by_id = {str(c.id or "").strip(): c for c in comments if (c.id or "").strip()}
        latest_by_root: dict[str, ReviewComment] = {}
        for c in comments:
            cid = (c.id or "").strip()
            if not cid:
                continue
            root_id = self._bbcloud_thread_root_comment_id(by_id, cid)
            current_latest = latest_by_root.get(root_id)
            if current_latest is None or self._bbcloud_comment_order_key(
                c
            ) >= self._bbcloud_comment_order_key(current_latest):
                latest_by_root[root_id] = c
        dismissed_roots = {
            root_id
            for root_id, comment in latest_by_root.items()
            if self._bbcloud_comment_is_bot_authored(comment, bot)
            and is_reply_dismissal_accepted_reply(comment.body)
        }
        return frozenset(f"comment:{root_id}" for root_id in dismissed_roots if root_id)

    @staticmethod
    def _bbcloud_comment_order_key(comment: ReviewComment) -> tuple[int, int, str]:
        raw_created_at = (comment.created_at or "").strip()
        timestamp_micros = 0
        if raw_created_at:
            try:
                normalized = (
                    raw_created_at[:-1] + "+00:00"
                    if raw_created_at.endswith("Z")
                    else raw_created_at
                )
                timestamp_micros = int(datetime.fromisoformat(normalized).timestamp() * 1_000_000)
            except ValueError:
                timestamp_micros = 0

        cid = (comment.id or "").strip()
        numeric_id = int(cid) if cid.isdigit() else -1
        return (timestamp_micros, numeric_id, cid.lower())

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
        """Bitbucket Cloud: unresolved inline PR comments plus open PR tasks.

        Inline comments use the same ``comment:{id}`` stable ids as the shared default helper
        so reply-dismissal gate exclusion can match quality-gate items.
        """
        out: list[UnresolvedReviewItem] = []
        try:
            comments = self.get_existing_review_comments(owner, repo, pr_number)
            dismissed_stable_ids = self._bbcloud_persisted_dismissed_root_ids(
                comments,
                self.get_bot_attribution_identity(owner, repo, pr_number),
            )
            inline_root_comments = [
                c for c in comments if self._bbcloud_is_inline_root_unresolved_comment(c)
            ]
            out.extend(
                item
                for item in default_unresolved_review_items_from_comments(inline_root_comments)
                if item.stable_id not in dismissed_stable_ids
            )
        except Exception as e:
            logger.warning(
                "Bitbucket Cloud PR comments fetch failed for quality gate "
                "owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
        url: str | None = self._path(owner, repo, "pullrequests", str(pr_number), "tasks")
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

    def _bbcloud_fetch_pr_comments_page(
        self, owner: str, repo: str, pr_number: int, url: str, visited: set[str]
    ) -> tuple[list[dict], str | None] | None:
        """Fetch one comments page. Returns None if the pagination loop should stop."""
        if not self._bitbucket_enter_next_link_page(url, visited):
            return None
        try:
            data = self._get(url)
        except Exception as e:
            logger.warning(
                "Bitbucket Cloud list comments failed owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return None
        if not isinstance(data, dict):
            return None
        page: list[dict] = []
        for c in data.get("values") or []:
            if isinstance(c, dict):
                page.append(c)
        nxt = data.get("next")
        next_url = nxt.strip() if isinstance(nxt, str) and nxt.strip() else None
        return page, next_url

    def _bbcloud_list_pr_comment_dicts(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        """Paginate GET pullrequest comments; return raw dicts (for reply-dismissal threading)."""
        url: str | None = self._path(owner, repo, "pullrequests", str(pr_number), "comments")
        out: list[dict] = []
        visited: set[str] = set()
        while url:
            chunk = self._bbcloud_fetch_pr_comments_page(owner, repo, pr_number, url, visited)
            if chunk is None:
                break
            page, next_url = chunk
            out.extend(page)
            url = next_url
        return out

    @staticmethod
    def _bbcloud_parent_id(c: dict) -> str | None:
        p = c.get("parent")
        if not isinstance(p, dict):
            return None
        if p.get("id") is not None:
            return str(p["id"])
        href = p.get("href")
        if isinstance(href, str) and "/comments/" in href:
            tail = href.rstrip("/").split("/")[-1]
            if tail.isdigit():
                return tail
        return None

    @staticmethod
    def _bbcloud_inline_path_line(c: dict) -> tuple[str, int]:
        inline = c.get("inline") if isinstance(c.get("inline"), dict) else {}
        path = str(inline.get("path") or "")
        try:
            line = int(inline.get("to") or inline.get("from") or 0)
        except (TypeError, ValueError):
            line = 0
        return path, line

    @staticmethod
    def _bbcloud_dismissal_meta(
        c: dict,
    ) -> tuple[str, str | None, str, str, str, str, int] | None:
        if not isinstance(c, dict):
            return None
        cid = str(c.get("id") or "").strip()
        if not cid:
            return None
        parent = BitbucketProvider._bbcloud_parent_id(c)
        content = c.get("content") if isinstance(c.get("content"), dict) else {}
        body = str(content.get("raw") or "")
        user = c.get("user") if isinstance(c.get("user"), dict) else {}
        login = str(
            user.get("nickname") or user.get("username") or user.get("display_name") or ""
        )
        created = str(c.get("created_on") or "")
        path, line = BitbucketProvider._bbcloud_inline_path_line(c)
        return (cid, parent, body, login, created, path, line)

    @staticmethod
    def _bbcloud_index_dismissal_by_id(
        raw_comments: list[dict],
    ) -> dict[str, dict[str, str | int | None]]:
        by_id: dict[str, dict[str, str | int | None]] = {}
        for c in raw_comments:
            meta = BitbucketProvider._bbcloud_dismissal_meta(c)
            if not meta:
                continue
            cid, parent, body, login, created, path, line = meta
            by_id[cid] = {
                "parent": parent,
                "body": body,
                "login": login,
                "created": created,
                "path": path,
                "line": line,
            }
        return by_id

    @staticmethod
    def _bbcloud_thread_root_from_want(
        by_id: dict[str, dict[str, str | int | None]], want: str
    ) -> str | None:
        if want not in by_id:
            return None
        root = want
        seen_up: set[str] = set()
        while root in by_id:
            par = by_id[root]["parent"]
            if not par or par not in by_id:
                break
            if root in seen_up:
                break
            seen_up.add(root)
            root = par
        return root

    @staticmethod
    def _bbcloud_thread_member_ids_sorted(
        by_id: dict[str, dict[str, str | int | None]], root: str
    ) -> list[str]:
        children: dict[str, list[str]] = {}
        for cid, info in by_id.items():
            par = info["parent"]
            if par and par in by_id:
                children.setdefault(par, []).append(cid)
        stack = [root]
        member_ids: list[str] = []
        seen_d: set[str] = set()
        while stack:
            n = stack.pop()
            if n in seen_d:
                continue
            seen_d.add(n)
            member_ids.append(n)
            stack.extend(children.get(n, []))
        member_ids.sort(key=lambda i: (by_id[i]["created"] or "", i))
        return member_ids

    @staticmethod
    def _bbcloud_dismissal_entries_from_members(
        by_id: dict[str, dict[str, str | int | None]], member_ids: list[str]
    ) -> list[ReviewThreadDismissalEntry]:
        entries: list[ReviewThreadDismissalEntry] = []
        for cid in member_ids:
            inf = by_id[cid]
            entries.append(
                ReviewThreadDismissalEntry(
                    comment_id=cid,
                    author_login=str(inf["login"] or ""),
                    body=str(inf["body"] or ""),
                    created_at=str(inf["created"] or ""),
                )
            )
        return entries

    @staticmethod
    def _bbcloud_build_dismissal_context(
        raw_comments: list[dict], triggered_comment_id: str
    ) -> ReviewThreadDismissalContext | None:
        want = (triggered_comment_id or "").strip()
        if not want:
            return None
        by_id = BitbucketProvider._bbcloud_index_dismissal_by_id(raw_comments)
        root = BitbucketProvider._bbcloud_thread_root_from_want(by_id, want)
        if root is None:
            return None
        member_ids = BitbucketProvider._bbcloud_thread_member_ids_sorted(by_id, root)
        entries = BitbucketProvider._bbcloud_dismissal_entries_from_members(by_id, member_ids)
        if len(entries) < 2:
            return None
        root_meta = by_id.get(root) or {}
        return ReviewThreadDismissalContext(
            gate_exclusion_stable_id=f"comment:{root}",
            path=str(root_meta.get("path") or ""),
            line=int(root_meta.get("line") or 0),
            entries=entries,
        )

    def get_review_thread_dismissal_context(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        triggered_comment_id: str,
    ) -> ReviewThreadDismissalContext | None:
        try:
            raw = self._bbcloud_list_pr_comment_dicts(owner, repo, pr_number)
            return self._bbcloud_build_dismissal_context(raw, triggered_comment_id)
        except Exception as e:
            logger.warning(
                "Bitbucket Cloud get_review_thread_dismissal_context failed "
                "owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return None

    def post_review_thread_reply(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        reply_to_comment_id: str,
        body: str,
    ) -> None:
        try:
            pid = int((reply_to_comment_id or "").strip())
        except ValueError as e:
            raise ValueError("Bitbucket Cloud reply_to_comment_id must be numeric") from e
        path = self._path(owner, repo, "pullrequests", str(pr_number), "comments")
        self._post(path, {"content": {"raw": body}, "parent": {"id": pid}})

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post PR-level comment (no inline)."""
        path = self._path(owner, repo, "pullrequests", str(pr_number), "comments")
        self._post(path, {"content": {"raw": body}})

    def get_bot_blocking_state(self, owner: str, repo: str, pr_number: int) -> BotBlockingState:
        """Inspect PR participants for the token user's approval / changes-requested state."""
        try:
            me = self._get(f"{self._base_url}/user")
            if not isinstance(me, dict):
                return "UNKNOWN"
            my_uuid = str(me.get("uuid") or "").strip()
            if not my_uuid:
                return "UNKNOWN"
            pr = self._get(self._path(owner, repo, "pullrequests", str(pr_number)))
            if not isinstance(pr, dict):
                return "UNKNOWN"
            participants = pr.get("participants") or []
        except Exception as e:
            logger.warning(
                "Bitbucket Cloud get_bot_blocking_state failed owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return "UNKNOWN"
        return _bitbucket_cloud_blocking_from_participants(participants, my_uuid)

    def get_bot_attribution_identity(
        self, owner: str, repo: str, pr_number: int
    ) -> BotAttributionIdentity:
        try:
            me = self._get(f"{self._base_url}/user")
            if isinstance(me, dict):
                uid = str(me.get("uuid") or "").strip()
                login = str(me.get("username") or "").strip().lower()
                return BotAttributionIdentity(login=login, uuid=uid)
        except Exception as e:
            logger.warning("Bitbucket Cloud get_bot_attribution_identity failed: %s", e)
        return BotAttributionIdentity()

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
            delete_soft_fail(
                self._delete,
                f"{base}/request-changes",
                log_label=f"Bitbucket Cloud clear request-changes for PR {pr_number}",
            )
            self._post(f"{base}/approve", {})
        else:
            delete_soft_fail(
                self._delete,
                f"{base}/approve",
                log_label=f"Bitbucket Cloud clear approve for PR {pr_number}",
            )
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
            embed_agent_marker_as_commonmark_linkref=True,
            supports_review_decisions=True,
            supports_bot_blocking_state_query=True,
            supports_bot_attribution_identity_query=True,
            supports_review_thread_dismissal_context=True,
            supports_review_thread_reply=True,
        )
