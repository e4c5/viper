"""Bitbucket Server / Data Center REST API 1.0 (project key = owner, repo slug = repo)."""

from typing import Any
import logging

import httpx

from code_review.diff.parser import parse_unified_diff
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
from code_review.providers.safety import truncate_repo_content

logger = logging.getLogger("code_review")

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
CONTENT_TYPE_JSON = "application/json"


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

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
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

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff for the PR (.diff endpoint)."""
        path = self._path(owner, repo, "pull-requests", str(pr_number), "diff")
        out = self._get(path)
        return out if isinstance(out, str) else ""

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
                        "get_file_content 404 for path=%s owner=%s repo=%s ref=%s (Bitbucket raw API)",
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

    def _get_pr_diff_refs(self, owner: str, repo: str, pr_number: int) -> tuple[str | None, str | None]:
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
                owner, repo, pr_number, e,
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
            payload = {"text": c.body, "anchor": anchor}
            try:
                self._post(path, payload)
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code == 409 and (
                    "fromHash" in anchor
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
                        k: v for k, v in anchor.items()
                        if k not in ("fromHash", "toHash", "diffType")
                    }
                    self._post(path, {"text": c.body, "anchor": anchor_simple})
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
        )

    def _comments_from_activities_page(self, data: Any) -> tuple[list[ReviewComment], int | None]:
        """Parse one activities API page. Returns (comments, next_start or None if no more)."""
        if not isinstance(data, dict):
            return [], None
        values = data.get("values") or []
        if not isinstance(values, list):
            return [], None
        comments = [
            c
            for act in values
            if (c := self._comment_from_activity(act)) is not None
        ]
        if data.get("isLastPage", True) or len(values) == 0:
            return comments, None
        next_start = data.get("nextPageStart")
        if next_start is None:
            return comments, None
        return comments, next_start

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

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post PR-level comment (no anchor)."""
        path = self._path(owner, repo, "pull-requests", str(pr_number), "comments")
        self._post(path, {"text": body})

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """Return PR title and description for skip-review. Labels from Server may vary."""
        try:
            path = self._path(owner, repo, "pull-requests", str(pr_number))
            data = self._get(path)
            if not isinstance(data, dict):
                return None
            title = data.get("title", "") or ""
            description = data.get("description", "") or ""
            return PRInfo(title=title, labels=[], description=description)
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
            supports_suggestions=False,
            markup_hides_html_comment=False,
            markup_supports_collapsible=False,
            omit_fingerprint_marker_in_body=True,
        )
