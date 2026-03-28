"""Bitbucket Server / Data Center REST API 1.0 (project key = owner, repo slug = repo)."""

import logging
from typing import Any
from urllib.parse import quote

import httpx

from code_review.diff.parser import parse_unified_diff
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
    _log_pr_commit_messages_warning,
    _log_pr_info_warning,
    commit_messages_from_commit_list,
    default_unresolved_review_items_from_comments,
    head_sha_from_pr_api_dict,
    normalize_diff_anchor_path,
)
from code_review.providers.safety import truncate_repo_content
from code_review.schemas.review_thread_dismissal import (
    ReviewThreadDismissalContext,
    ReviewThreadDismissalEntry,
)

logger = logging.getLogger("code_review")


def _is_truthy_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _http_error_response_text(exc: httpx.HTTPStatusError, limit: int = 2000) -> str:
    """Best-effort response body snippet for logging (Bitbucket Server review decision paths)."""
    try:
        if exc.response is not None:
            return (exc.response.text or "")[:limit]
    except Exception:
        pass
    return ""


def _bbs_blocking_state_for_one_entry(
    rev: dict, want_slug_lower: str
) -> BotBlockingState | None:
    user = rev.get("user") or {}
    if not isinstance(user, dict):
        return None
    uslug = str(user.get("slug") or "").strip().lower()
    if uslug != want_slug_lower:
        return None
    status = str(rev.get("status") or "").upper()
    if status == "NEEDS_WORK":
        return "BLOCKING"
    if status == "APPROVED":
        return "NOT_BLOCKING"
    if rev.get("approved") is True:
        return "NOT_BLOCKING"
    return "NOT_BLOCKING"


def _bbs_blocking_state_from_user_entries(
    entries: list[Any] | None, want_slug_lower: str
) -> BotBlockingState | None:
    """Blocking state for ``want_slug_lower`` in PR participant/reviewer rows, or None if absent."""
    for rev in entries or []:
        if not isinstance(rev, dict):
            continue
        hit = _bbs_blocking_state_for_one_entry(rev, want_slug_lower)
        if hit is not None:
            return hit
    return None


MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
CONTENT_TYPE_JSON = "application/json"
_DEV_NULL = "/dev/null"


def _diff_file_headers(src_path: str, dst_path: str) -> list[str]:
    """Return the three header lines for one file in a unified diff.

    Produces::

        diff --git a/<path> b/<path>
        --- a/<src>  (or --- /dev/null for new files)
        +++ b/<dst>  (or +++ /dev/null for deleted files)

    The ``diff --git`` header is required by :func:`parse_unified_diff` so it can
    flush the previous file's hunks before starting a new file.  For deleted files
    the effective path is taken from the source so the diff is still attributed
    correctly.
    """
    src_header = _DEV_NULL if src_path == _DEV_NULL else f"a/{src_path}"
    dst_header = _DEV_NULL if dst_path == _DEV_NULL else f"b/{dst_path}"
    effective_path = dst_path if dst_path != _DEV_NULL else src_path
    return [
        f"diff --git a/{effective_path} b/{effective_path}",
        f"--- {src_header}",
        f"+++ {dst_header}",
    ]


def _hunk_header(hunk: dict) -> str:
    """Return the ``@@ -old +new @@`` header line for one hunk dict."""
    src_start = hunk.get("sourceLine", 0)
    src_span = hunk.get("sourceSpan", 0)
    dst_start = hunk.get("destinationLine", 0)
    dst_span = hunk.get("destinationSpan", 0)
    return f"@@ -{src_start},{src_span} +{dst_start},{dst_span} @@"


def _segment_lines(segment: dict) -> list[str]:
    """Return unified-diff lines for one Bitbucket Server diff segment.

    Each segment has a ``type`` (``"ADDED"``, ``"REMOVED"``, or ``"CONTEXT"``)
    and a ``lines`` list of ``{"line": "<content>"}`` dicts.
    """
    seg_type = segment.get("type", "CONTEXT")
    _prefix = {"ADDED": "+", "REMOVED": "-"}
    prefix = _prefix.get(seg_type, " ")
    return [f"{prefix}{entry.get('line', '')}" for entry in segment.get("lines") or []]


def _bitbucket_json_diff_to_unified(data: dict) -> str:
    """Convert a Bitbucket Server JSON diff response to unified diff format.

    Bitbucket Server GET /diff returns a structured JSON object with a ``diffs``
    array instead of a standard unified diff text.  The JSON has the shape::

        {
            "diffs": [
                {
                    "source":      {"toString": "old/path.java", ...},
                    "destination": {"toString": "new/path.java", ...},
                    "hunks": [
                        {
                            "sourceLine": 1, "sourceSpan": 3,
                            "destinationLine": 1, "destinationSpan": 4,
                            "segments": [
                                {"type": "CONTEXT", "lines": [{"line": "...", ...}]},
                                {"type": "ADDED",   "lines": [{"line": "...", ...}]},
                                {"type": "REMOVED", "lines": [{"line": "...", ...}]},
                            ]
                        }
                    ]
                }
            ]
        }

    This function converts that structure to the unified diff format expected by
    :func:`~code_review.diff.parser.parse_unified_diff`.
    """
    output: list[str] = []
    for file_diff in data.get("diffs") or []:
        src_path = (file_diff.get("source") or {}).get("toString") or _DEV_NULL
        dst_path = (file_diff.get("destination") or {}).get("toString") or _DEV_NULL
        output.extend(_diff_file_headers(src_path, dst_path))
        for hunk in file_diff.get("hunks") or []:
            output.append(_hunk_header(hunk))
            for segment in hunk.get("segments") or []:
                output.extend(_segment_lines(segment))
    return "\n".join(output)


def _extract_commit_id(ref: dict) -> str | None:
    """Extract the commit hash from a Bitbucket Server ref object.

    The Bitbucket Server REST API returns ``latestCommit`` as a plain string
    hash (e.g. ``"abc123def456..."``) rather than a nested dict.  Earlier code
    assumed it was a dict with an ``id`` key, which caused:
    ``'str' object has no attribute 'get'``.
    """
    latest = ref.get("latestCommit")
    if isinstance(latest, str) and latest:
        return latest
    if isinstance(latest, dict):
        return latest.get("id") or None
    return ref.get("id") or None


class BitbucketServerProvider(ProviderInterface):
    """Bitbucket Server / Data Center REST API 1.0 client for PR diff, file content, and comments.

    Use SCM_PROVIDER=bitbucket_server and SCM_URL with the REST API base including /rest/api/1.0,
    e.g. http://localhost:7990/rest/api/1.0 (no trailing slash).
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        *,
        participant_user_slug: str = "",
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._participant_user_slug = (participant_user_slug or "").strip()
        # Cache (owner, repo, ref, path) combinations that returned 404 from the raw API
        # so we don't hammer Bitbucket or spam logs when the LLM repeatedly asks for
        # content of a file that doesn't exist at this ref (e.g. deleted/renamed files).
        self._missing_files: set[tuple[str, str, str, str]] = set()

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    def _path(self, owner: str, repo: str, *parts: str) -> str:
        return f"{self._base_url}/projects/{owner}/repos/{repo}/" + "/".join(parts)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(path, headers=self._headers(), params=params or {})
            r.raise_for_status()
            if CONTENT_TYPE_JSON in (r.headers.get("content-type") or ""):
                return r.json()
            return r.text

    def _get_raw_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(path, headers=self._headers(), params=params or {})
            r.raise_for_status()
            return r.content

    def _post(self, path: str, json: dict) -> Any:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(
                path,
                headers={**self._headers(), "Content-Type": CONTENT_TYPE_JSON},
                json=json,
            )
            r.raise_for_status()
            return r.json() if r.content else None

    def _put(self, path: str, json: dict) -> Any:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.put(
                path,
                headers={**self._headers(), "Content-Type": CONTENT_TYPE_JSON},
                json=json,
            )
            r.raise_for_status()
            return r.json() if r.content else None

    @staticmethod
    def _next_diff_page_start(data: dict[str, Any], current_start: int) -> int | None:
        if bool(data.get("isLastPage", True)):
            return None
        nxt = data.get("nextPageStart")
        if nxt is None:
            return None
        try:
            next_start = int(nxt)
        except (TypeError, ValueError):
            return None
        if next_start == current_start:
            return None
        return next_start

    def _merge_paginated_diff_pages(
        self,
        path: str,
        first_page: dict[str, Any],
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = dict(first_page)
        merged["diffs"] = list(first_page.get("diffs") or [])
        page_params = dict(params or {})
        try:
            start = int(page_params.get("start", 0) or 0)
        except (TypeError, ValueError):
            start = 0
        for _ in range(500):
            start = self._next_diff_page_start(first_page, start)
            if start is None:
                break
            page_params["start"] = start
            first_page = self._get(path, params=page_params)
            if not isinstance(first_page, dict):
                break
            merged["diffs"].extend(first_page.get("diffs") or [])
        return merged

    def _get_unified_diff(self, path: str, params: dict[str, Any] | None = None) -> str:
        out = self._get(path, params=params)
        if isinstance(out, str):
            return out
        if isinstance(out, dict) and "diffs" in out:
            return _bitbucket_json_diff_to_unified(
                self._merge_paginated_diff_pages(path, out, params=params)
            )
        return ""

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff for the PR (.diff endpoint).

        Bitbucket Server returns a JSON diff object from this endpoint rather
        than unified diff text.  When a JSON response is detected the structured
        diff is converted to unified diff format via
        :func:`_bitbucket_json_diff_to_unified` so the rest of the codebase can
        parse it normally.
        """
        path = self._path(owner, repo, "pull-requests", str(pr_number), "diff")
        return self._get_unified_diff(path)

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
        path = self._path(owner, repo, "compare", "diff")
        return self._get_unified_diff(path, params={"from": base_sha, "to": head_sha})

    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Return file content at ref (raw endpoint with at=ref)."""
        cache_key = (owner, repo, str(ref), path)
        if cache_key in self._missing_files:
            return ""
        url = self._path(owner, repo, "raw", path.lstrip("/"))
        try:
            raw = self._get_raw_bytes(url, params={"at": ref})
        except httpx.HTTPStatusError as e:
            # Treat 404 as "file not present at this ref" instead of failing the whole run.
            # This can happen for renamed/deleted paths or when diff paths don't exist at `ref`.
            if e.response is not None and e.response.status_code == 404:
                if cache_key not in self._missing_files:
                    self._missing_files.add(cache_key)
                    logger.warning(
                        "get_file_content 404 for path=%s owner=%s repo=%s ref=%s "
                        "(Bitbucket raw API)",
                        path,
                        owner,
                        repo,
                        ref,
                    )
                return ""
            raise
        text = raw.decode("utf-8", errors="replace")
        return truncate_repo_content(text, max_bytes=MAX_REPO_FILE_BYTES)

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        """Return list of changed files by parsing the PR diff."""
        diff_text = self.get_pr_diff(owner, repo, pr_number)
        return self._file_infos_from_diff_text(diff_text)

    def get_incremental_pr_files(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> list[FileInfo]:
        """Return changed files in the incremental compare range ``base_sha..head_sha``."""
        if not base_sha or not head_sha or base_sha == head_sha:
            return self.get_pr_files(owner, repo, pr_number)
        return self._file_infos_from_diff_text(
            self.get_incremental_pr_diff(owner, repo, pr_number, base_sha, head_sha)
        )

    def _file_infos_from_diff_text(self, diff_text: str) -> list[FileInfo]:
        """Return FileInfo objects by parsing a unified diff string."""
        seen: set[str] = set()
        result: list[FileInfo] = []
        for hunk in parse_unified_diff(diff_text):
            if not hunk.path:
                continue
            path = self._anchor_path_for_diff(hunk.path)
            if not path or path in seen:
                continue
            if "node_modules" in path:
                continue
            seen.add(path)
            result.append(FileInfo(path=path, status="modified", additions=0, deletions=0))
        return result

    def _anchor_path_for_diff(self, file_path: str) -> str:
        """Normalize path so it matches the PR diff (enables inline comments on the diff view)."""
        return normalize_diff_anchor_path(file_path)

    def _get_pr_diff_refs(
        self, owner: str, repo: str, pr_number: int
    ) -> tuple[str | None, str | None]:
        """Get (from_hash, to_hash) for the PR diff. Used for inline comment anchors.
        Returns (None, None) if the PR cannot be read or hashes are missing."""
        try:
            pr_path = self._path(owner, repo, "pull-requests", str(pr_number))
            data = self._get(pr_path)
            if not isinstance(data, dict):
                return (None, None)
            from_ref = data.get("fromRef") or {}
            to_ref = data.get("toRef") or {}
            from_id = _extract_commit_id(from_ref)
            to_id = _extract_commit_id(to_ref)
            return (from_id, to_id)
        except Exception as e:
            logger.warning(
                "_get_pr_diff_refs failed owner=%s repo=%s pr_number=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return (None, None)

    def post_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        comments: list[InlineComment],
        head_sha: str = "",
    ) -> None:
        """Post inline comments (Server API: text + anchor with path/line/lineType/fileType/refs).
        Path is normalized. lineType must match the line in the diff: ADDED for '+' lines, CONTEXT
        for unchanged lines; otherwise Bitbucket Server may show the comment at file level instead
        of on the line.

        Bitbucket Server expects anchor commit range direction to match fileType:
        for fileType="TO", fromHash must be the destination/base commit and toHash
        must be the source/head commit. Using the opposite direction triggers 409
        PullRequestOutOfDateException on Data Center.

        When fromHash equals the target branch HEAD rather than the merge-base (which happens
        when the target branch has advanced after the PR was created), Bitbucket Server returns
        409 because the anchor does not match the PR's effective diff. In that case this method
        retries with a simplified anchor that omits fromHash/toHash/diffType so the server can
        resolve the correct merge-base automatically."""
        if not comments:
            return
        path = self._path(owner, repo, "pull-requests", str(pr_number), "comments")
        source_hash, target_hash = self._get_pr_diff_refs(owner, repo, pr_number)
        if source_hash is None and head_sha:
            source_hash = head_sha
        for c in comments:
            anchor_path = self._anchor_path_for_diff(c.path)
            line_type = getattr(c, "line_type", None) or "ADDED"
            anchor: dict[str, Any] = {
                "path": anchor_path,
                "line": c.line,
                "lineType": line_type,
                "fileType": "TO",
            }
            # For fileType="TO", Bitbucket Server expects base->head direction.
            if source_hash and target_hash:
                anchor["fromHash"] = target_hash
                anchor["toHash"] = source_hash
                anchor["diffType"] = "EFFECTIVE"
            payload = {
                "text": render_suggestion_block(c.body, c.suggested_patch),
                "anchor": anchor,
            }
            try:
                self._post(path, payload)
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response is not None
                    and exc.response.status_code == 409
                    and ("fromHash" in anchor)
                ):
                    # The anchor's fromHash (toRef.latestCommit) may not be the PR's merge-base
                    # when the target branch has advanced.  Retry without the optional hash
                    # fields so Bitbucket Server can compute the correct merge-base itself.
                    logger.debug(
                        "post_review_comment 409 with hashes, retrying without: "
                        "owner=%s repo=%s pr_number=%s path=%s line=%s",
                        owner,
                        repo,
                        pr_number,
                        c.path,
                        c.line,
                    )
                    anchor_simple: dict[str, Any] = {
                        k: v
                        for k, v in anchor.items()
                        if k not in ("fromHash", "toHash", "diffType")
                    }
                    self._post(
                        path,
                        {
                            "text": render_suggestion_block(c.body, c.suggested_patch),
                            "anchor": anchor_simple,
                        },
                    )
                else:
                    raise

    def _comment_from_activity(self, act: dict) -> ReviewComment | None:
        """Parse one activity entry into a ReviewComment if it is a COMMENTED action."""
        if not isinstance(act, dict) or act.get("action") != "COMMENTED":
            return None
        c = act.get("comment")
        if not isinstance(c, dict):
            return None
        anchor = c.get("anchor") or {}
        return ReviewComment(
            id=str(c.get("id", "")),
            path=anchor.get("path") or "",
            line=int(anchor.get("line", 0) or 0),
            body=c.get("text") or "",
            resolved=bool(c.get("state") == "RESOLVED"),
            outdated=self._bbs_comment_is_outdated(c),
        )

    @staticmethod
    def _bbs_comment_is_outdated(comment: dict[str, Any]) -> bool:
        """Return True when Bitbucket marks the comment anchor as out-of-date/orphaned.

        Bitbucket Server/DC models outdated PR comments as orphaned diff anchors.
        Different API surfaces may expose that as ``anchor.orphaned``/``anchor.isOrphaned``
        booleans or an ``ORPHANED`` anchor state string, so we accept the common shapes.
        """
        anchor = comment.get("anchor")
        if not isinstance(anchor, dict):
            return False
        for key in ("orphaned", "isOrphaned"):
            if _is_truthy_flag(anchor.get(key)):
                return True
        for key in ("state", "anchorState"):
            state = str(anchor.get(key) or "").strip().upper()
            if state == "ORPHANED":
                return True
        return False

    def _comments_from_activities_page(self, data: Any) -> tuple[list[ReviewComment], int | None]:
        """Parse one activities API page. Returns (comments, next_start or None if no more)."""
        if not isinstance(data, dict):
            return [], None
        values = data.get("values") or []
        if not isinstance(values, list):
            return [], None
        comments = [c for act in values if (c := self._comment_from_activity(act)) is not None]
        if data.get("isLastPage", True) or len(values) == 0:
            return comments, None
        next_start = data.get("nextPageStart")
        if next_start is None:
            return comments, None
        return comments, next_start

    @staticmethod
    def _bbs_merge_nested_comment_dicts_into(
        by_id: dict[str, dict],
        comment: Any,
        *,
        parent_id: str | None = None,
    ) -> None:
        if not isinstance(comment, dict):
            return
        cid = str(comment.get("id") or "").strip()
        synthesized_parent = parent_id.strip() if isinstance(parent_id, str) else ""
        current = dict(by_id.get(cid) or {}) if cid else {}
        current.update(comment)
        if synthesized_parent and not BitbucketServerProvider._bbs_comment_parent_id(current):
            current["parentComment"] = {"id": synthesized_parent}
        if cid:
            by_id[cid] = current
        child_parent_id = cid or synthesized_parent or None
        for child in comment.get("comments") or []:
            BitbucketServerProvider._bbs_merge_nested_comment_dicts_into(
                by_id,
                child,
                parent_id=child_parent_id,
            )

    @staticmethod
    def _bbs_merge_commented_activities_into(by_id: dict[str, dict], data: dict) -> None:
        for act in data.get("values") or []:
            if not isinstance(act, dict) or act.get("action") != "COMMENTED":
                continue
            c = act.get("comment")
            if not isinstance(c, dict):
                continue
            BitbucketServerProvider._bbs_merge_nested_comment_dicts_into(by_id, c)

    @staticmethod
    def _bbs_activities_next_start(data: dict, start: int) -> int | None:
        if data.get("isLastPage", True):
            return None
        nxt = data.get("nextPageStart")
        if nxt is None:
            return None
        try:
            next_start = int(nxt)
        except (TypeError, ValueError):
            return None
        if next_start == start:
            return None
        return next_start

    def _bbs_list_comment_dicts_from_activities(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict] | None:
        """Collect unique PR comment dicts from paginated activities (COMMENTED)."""
        path = self._path(owner, repo, "pull-requests", str(pr_number), "activities")
        by_id: dict[str, dict] = {}
        start = 0
        for _ in range(500):
            try:
                data = self._get(path, params={"start": start, "limit": 100})
            except Exception as e:
                logger.warning(
                    "Bitbucket Server activities fetch for dismissal owner=%s repo=%s pr=%s: %s",
                    owner,
                    repo,
                    pr_number,
                    e,
                )
                return None
            if not isinstance(data, dict):
                return None
            self._bbs_merge_commented_activities_into(by_id, data)
            nxt = BitbucketServerProvider._bbs_activities_next_start(data, start)
            if nxt is None:
                break
            start = nxt
        return list(by_id.values())

    @staticmethod
    def _bbs_comment_parent_id(c: dict) -> str | None:
        for key in ("parentComment", "parent"):
            p = c.get(key)
            if isinstance(p, dict) and p.get("id") is not None:
                return str(p["id"])
        return None

    @staticmethod
    def _bbs_dismissal_meta(c: dict) -> tuple[str, str | None, str, str, int] | None:
        if not isinstance(c, dict):
            return None
        cid = str(c.get("id") or "").strip()
        if not cid:
            return None
        parent = BitbucketServerProvider._bbs_comment_parent_id(c)
        body = str(c.get("text") or c.get("body") or "")
        author = c.get("author") if isinstance(c.get("author"), dict) else {}
        login = str(author.get("name") or author.get("slug") or author.get("username") or "")
        cd = c.get("createdDate")
        try:
            ts = int(cd) if cd is not None else 0
        except (TypeError, ValueError):
            ts = 0
        return (cid, parent, body, login, ts)

    @staticmethod
    def _bbs_index_dismissal_by_id(
        raw_comments: list[dict],
    ) -> dict[str, dict[str, str | None | int]]:
        by_id: dict[str, dict[str, str | None | int]] = {}
        for c in raw_comments:
            meta = BitbucketServerProvider._bbs_dismissal_meta(c)
            if not meta:
                continue
            cid, parent, body, login, ts = meta
            by_id[cid] = {
                "parent": parent,
                "body": body,
                "login": login,
                "ts": ts,
            }
        return by_id

    @staticmethod
    def _bbs_thread_root_from_want(
        by_id: dict[str, dict[str, str | None | int]], want: str
    ) -> str | None:
        if want not in by_id:
            return None
        root = want
        seen_up: set[str] = set()
        while root in by_id:
            par = by_id[root]["parent"]
            if not par or str(par) not in by_id or root in seen_up:
                break
            seen_up.add(root)
            root = str(par)
        return root

    @staticmethod
    def _bbs_thread_member_ids_sorted(
        by_id: dict[str, dict[str, str | None | int]], root: str
    ) -> list[str]:
        children: dict[str, list[str]] = {}
        for cid, info in by_id.items():
            par = info["parent"]
            if par and str(par) in by_id:
                children.setdefault(str(par), []).append(cid)
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
        member_ids.sort(key=lambda i: (int(by_id[i]["ts"]), i))
        return member_ids

    @staticmethod
    def _bbs_dismissal_entries_from_members(
        by_id: dict[str, dict[str, str | None | int]], member_ids: list[str]
    ) -> list[ReviewThreadDismissalEntry]:
        entries: list[ReviewThreadDismissalEntry] = []
        for cid in member_ids:
            inf = by_id[cid]
            entries.append(
                ReviewThreadDismissalEntry(
                    comment_id=cid,
                    author_login=str(inf["login"] or ""),
                    body=str(inf["body"] or ""),
                    created_at=str(inf["ts"]),
                )
            )
        return entries

    @staticmethod
    def _bbs_build_dismissal_context(
        raw_comments: list[dict], triggered_comment_id: str
    ) -> ReviewThreadDismissalContext | None:
        want = (triggered_comment_id or "").strip()
        if not want:
            return None
        by_id = BitbucketServerProvider._bbs_index_dismissal_by_id(raw_comments)
        root = BitbucketServerProvider._bbs_thread_root_from_want(by_id, want)
        if root is None:
            return None
        member_ids = BitbucketServerProvider._bbs_thread_member_ids_sorted(by_id, root)
        entries = BitbucketServerProvider._bbs_dismissal_entries_from_members(by_id, member_ids)
        if len(entries) < 2:
            return None
        return ReviewThreadDismissalContext(
            gate_exclusion_stable_id=f"comment:{root}",
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
            raw = self._bbs_list_comment_dicts_from_activities(owner, repo, pr_number)
            if raw is None:
                return None
            return self._bbs_build_dismissal_context(raw, triggered_comment_id)
        except Exception as e:
            logger.warning(
                "Bitbucket Server get_review_thread_dismissal_context failed "
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
            raise ValueError("Bitbucket Server reply_to_comment_id must be numeric") from e
        path = self._path(owner, repo, "pull-requests", str(pr_number), "comments")
        self._post(path, {"text": body, "parent": {"id": pid}})

    def get_existing_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewComment]:
        """Return existing PR comments via the activities endpoint (Bitbucket Server 7+).

        The GET .../comments endpoint may return 404 on some Server/DC versions; activities
        with action COMMENTED are the supported way to list comments.
        """
        path = self._path(owner, repo, "pull-requests", str(pr_number), "activities")
        result: list[ReviewComment] = []
        start = 0
        max_pages = 500  # safeguard against infinite loop
        for _ in range(max_pages):
            data = self._get(path, params={"start": start, "limit": 100})
            comments, next_start = self._comments_from_activities_page(data)
            result.extend(comments)
            if next_start is None or next_start == start:
                break
            start = next_start
        return result

    @staticmethod
    def _bbs_append_open_task_for_quality_gate(t: Any, items: list[UnresolvedReviewItem]) -> None:
        if not isinstance(t, dict):
            return
        state = (str(t.get("state") or "")).strip().upper()
        if state in ("RESOLVED", "DECLINED"):
            return
        text = str(t.get("text") or "").strip()
        if not text:
            return
        tid = str(t.get("id", "") or "")
        items.append(
            UnresolvedReviewItem(
                stable_id=f"bbs:task:{tid}" if tid else f"bbs:task:{len(items)}",
                thread_id=tid or None,
                kind="task",
                path="",
                line=0,
                body=text,
                inferred_severity=infer_severity_from_comment_body(text),
            )
        )

    def _bbs_paginate_open_pr_tasks(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        items: list[UnresolvedReviewItem],
    ) -> None:
        path = self._path(owner, repo, "pull-requests", str(pr_number), "tasks")
        start = 0
        for _ in range(500):
            try:
                data = self._get(path, params={"start": start, "limit": 50})
            except Exception as e:
                logger.warning(
                    "Bitbucket Server PR tasks failed owner=%s repo=%s pr=%s: %s",
                    owner,
                    repo,
                    pr_number,
                    e,
                )
                return
            if not isinstance(data, dict):
                return
            for t in data.get("values") or []:
                self._bbs_append_open_task_for_quality_gate(t, items)
            if bool(data.get("isLastPage", True)):
                return
            nxt = data.get("nextPageStart")
            if nxt is None:
                return
            next_start = int(nxt)
            if next_start == start:
                return
            start = next_start

    def get_unresolved_review_items_for_quality_gate(
        self, owner: str, repo: str, pr_number: int
    ) -> list[UnresolvedReviewItem]:
        """Unresolved inline comments (non-RESOLVED) plus open PR tasks."""
        try:
            existing = self.get_existing_review_comments(owner, repo, pr_number)
        except Exception as e:
            logger.warning(
                "Bitbucket Server PR activities fetch failed for quality gate "
                "owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            existing = []
        items = list(default_unresolved_review_items_from_comments(existing))
        self._bbs_paginate_open_pr_tasks(owner, repo, pr_number, items)
        return items

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post PR-level comment (no anchor)."""
        path = self._path(owner, repo, "pull-requests", str(pr_number), "comments")
        self._post(path, {"text": body})

    def _pull_request_version(self, owner: str, repo: str, pr_number: int) -> int | None:
        """Return PR ``version`` for optimistic locking on participant updates."""
        path = self._path(owner, repo, "pull-requests", str(pr_number))
        try:
            data = self._get(path)
        except Exception as e:
            logger.warning(
                "Bitbucket Server GET pull request failed owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return None
        if not isinstance(data, dict):
            return None
        raw = data.get("version")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _bbs_participant_put(
        self, participant_path: str, payload: dict[str, str], pr_version: int
    ) -> None:
        self._put(f"{participant_path}?version={pr_version}", payload)

    def _bbs_submit_review_decision_retry_after_409(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        participant_path: str,
        payload: dict[str, str],
        cause: httpx.HTTPStatusError,
    ) -> None:
        """Refetch PR version and retry participant PUT once after HTTP 409."""
        version2 = self._pull_request_version(owner, repo, pr_number)
        if version2 is None:
            raise ValueError(
                "Bitbucket Server: could not read pull request version "
                "after 409 on review decision."
            ) from cause
        logger.debug(
            "Bitbucket Server submit_review_decision 409; retrying participant PUT "
            "owner=%s repo=%s pr=%s",
            owner,
            repo,
            pr_number,
        )
        self._bbs_participant_put(participant_path, payload, version2)

    def _bbs_try_participant_put_after_refetch(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        participant_path: str,
        payload: dict[str, str],
        version2: int,
    ) -> bool:
        """PUT with a refetched PR version.

        True if succeeded; False if HTTP 400 (caller may try -1).
        """
        try:
            self._bbs_participant_put(participant_path, payload, version2)
            return True
        except httpx.HTTPStatusError as exc2:
            sc2 = exc2.response.status_code if exc2.response is not None else 0
            if sc2 == 409:
                version3 = self._pull_request_version(owner, repo, pr_number)
                if version3 is None:
                    raise ValueError(
                        "Bitbucket Server: could not read pull request version "
                        "after second 409 on review decision."
                    ) from exc2
                self._bbs_participant_put(participant_path, payload, version3)
                return True
            if sc2 != 400:
                raise
            return False

    def _bbs_submit_review_decision_handle_400(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        participant_path: str,
        payload: dict[str, str],
        version: int,
        exc: httpx.HTTPStatusError,
    ) -> None:
        """Recover from HTTP 400: retry with refetched version and/or version=-1 wildcard."""
        err_text = _http_error_response_text(exc)
        logger.warning(
            "Bitbucket Server submit_review_decision HTTP 400 owner=%s repo=%s pr=%s "
            "participant_slug=%s first_version=%s response=%s",
            owner,
            repo,
            pr_number,
            self._participant_user_slug,
            version,
            err_text,
        )
        version2 = self._pull_request_version(owner, repo, pr_number)
        if (
            version2 is not None
            and version2 != version
            and self._bbs_try_participant_put_after_refetch(
                owner, repo, pr_number, participant_path, payload, version2
            )
        ):
            return
        try:
            self._bbs_participant_put(participant_path, payload, -1)
        except httpx.HTTPStatusError as exc3:
            logger.warning(
                "Bitbucket Server participant PUT with version=-1 failed "
                "owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                _http_error_response_text(exc3),
            )
            raise exc3

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
        """Set the token user's participant status (``APPROVED`` / ``NEEDS_WORK``).

        Requires ``SCM_BITBUCKET_SERVER_USER_SLUG`` (``participant_user_slug``) to match the
        authenticated user's username slug. ``body`` and ``head_sha`` are ignored (participant
        PUT does not carry a review summary in this client).
        """
        _ = body, head_sha
        if not self._participant_user_slug:
            raise ValueError(
                "Bitbucket Server review decisions require SCM_BITBUCKET_SERVER_USER_SLUG "
                "(username slug of the API token user)."
            )
        version = self._pull_request_version(owner, repo, pr_number)
        if version is None:
            raise ValueError(
                "Bitbucket Server: could not read pull request version for review decision."
            )
        slug = quote(self._participant_user_slug, safe="")
        status = "APPROVED" if decision == "APPROVE" else "NEEDS_WORK"
        participant_path = self._path(
            owner, repo, "pull-requests", str(pr_number), "participants", slug
        )
        payload = {"status": status}

        try:
            self._bbs_participant_put(participant_path, payload, version)
        except httpx.HTTPStatusError as exc:
            sc = exc.response.status_code if exc.response is not None else 0
            if sc == 409:
                self._bbs_submit_review_decision_retry_after_409(
                    owner, repo, pr_number, participant_path, payload, exc
                )
                return
            if sc == 400:
                self._bbs_submit_review_decision_handle_400(
                    owner, repo, pr_number, participant_path, payload, version, exc
                )
                return
            raise

    def get_pr_commit_messages(self, owner: str, repo: str, pr_number: int) -> list[str]:
        """List commits on the pull request (paginated REST)."""
        path = self._path(owner, repo, "pull-requests", str(pr_number), "commits")
        out: list[str] = []
        start = 0
        for _ in range(500):
            data = self._safe_get_commit_page(path, start, owner, repo, pr_number)
            if data is None:
                return out
            out.extend(commit_messages_from_commit_list(data.get("values")))
            start = self._next_commit_page_start(data, current_start=start)
            if start is None:
                break
        return out

    def _safe_get_commit_page(
        self,
        path: str,
        start: int,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> dict[str, Any] | None:
        try:
            data = self._get(path, params={"start": start, "limit": 50})
        except Exception as e:
            _log_pr_commit_messages_warning(logger, owner, repo, pr_number, e)
            return None
        if not isinstance(data, dict):
            return None
        return data

    @staticmethod
    def _next_commit_page_start(data: dict[str, Any], current_start: int) -> int | None:
        if bool(data.get("isLastPage", True)):
            return None
        nxt = data.get("nextPageStart")
        if nxt is None:
            return None
        next_start = int(nxt)
        if next_start == current_start:
            return None
        return next_start

    def get_bot_blocking_state(self, owner: str, repo: str, pr_number: int) -> BotBlockingState:
        """Use PR ``participants`` first (participant PUT), then ``reviewers``."""
        if not self._participant_user_slug:
            return "UNKNOWN"
        want = self._participant_user_slug.strip().lower()
        try:
            data = self._get(self._path(owner, repo, "pull-requests", str(pr_number)))
        except Exception as e:
            logger.warning(
                "Bitbucket Server get_bot_blocking_state failed owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return "UNKNOWN"
        if not isinstance(data, dict):
            return "UNKNOWN"
        inspected = False
        participants = data.get("participants")
        if participants is not None:
            inspected = True
            hit = _bbs_blocking_state_from_user_entries(participants, want)
            if hit is not None:
                return hit
        reviewers = data.get("reviewers")
        if reviewers is not None:
            inspected = True
            hit = _bbs_blocking_state_from_user_entries(reviewers, want)
            if hit is not None:
                return hit
        if not inspected:
            return "UNKNOWN"
        return "NOT_BLOCKING"

    def get_bot_attribution_identity(
        self, owner: str, repo: str, pr_number: int
    ) -> BotAttributionIdentity:
        slug = (self._participant_user_slug or "").strip().lower()
        if slug:
            return BotAttributionIdentity(slug=slug)
        return BotAttributionIdentity()

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """Return PR title and description for skip-review. Labels from Server may vary."""
        try:
            path = self._path(owner, repo, "pull-requests", str(pr_number))
            data = self._get(path)
            if not isinstance(data, dict):
                return None
            title = data.get("title", "") or ""
            description = data.get("description", "") or ""
            return PRInfo(
                title=title,
                labels=[],
                description=description,
                head_sha=head_sha_from_pr_api_dict(data),
            )
        except Exception as e:
            _log_pr_info_warning(logger, owner, repo, pr_number, e)
            return None

    def update_pr_description(
        self, owner: str, repo: str, pr_number: int, description: str, title: str | None = None
    ) -> None:
        """Update the PR description via PUT pull-requests/:id."""
        path = self._path(owner, repo, "pull-requests", str(pr_number))
        payload: dict = {"description": description}
        if title is not None:
            payload["title"] = title
        self._put(path, payload)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=True,
            markup_hides_html_comment=False,
            markup_supports_collapsible=False,
            omit_fingerprint_marker_in_body=True,
            embed_agent_marker_as_commonmark_linkref=True,
            supports_review_decisions=bool(self._participant_user_slug),
            supports_bot_blocking_state_query=bool(self._participant_user_slug),
            supports_bot_attribution_identity_query=bool(self._participant_user_slug),
            supports_review_thread_dismissal_context=True,
            supports_review_thread_reply=True,
        )
