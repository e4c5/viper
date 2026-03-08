"""Bitbucket Server / Data Center REST API 1.0 (project key = owner, repo slug = repo)."""

from typing import Any

import httpx

from code_review.diff.parser import parse_unified_diff
from code_review.providers.base import (
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    ProviderInterface,
    ReviewComment,
    pr_info_from_api_dict,
)
from code_review.providers.safety import truncate_repo_content

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB


class BitbucketServerProvider(ProviderInterface):
    """Bitbucket Server / Data Center REST API 1.0 client for PR diff, file content, and comments.

    Use SCM_PROVIDER=bitbucket_server and SCM_URL with the REST API base including /rest/api/1.0,
    e.g. http://localhost:7990/rest/api/1.0 (no trailing slash).
    """

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

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
            if "application/json" in (r.headers.get("content-type") or ""):
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
                headers={**self._headers(), "Content-Type": "application/json"},
                json=json,
            )
            r.raise_for_status()
            return r.json() if r.content else None

    def _put(self, path: str, json: dict) -> Any:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.put(
                path,
                headers={**self._headers(), "Content-Type": "application/json"},
                json=json,
            )
            r.raise_for_status()
            return r.json() if r.content else None

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff for the PR (.diff endpoint)."""
        path = self._path(owner, repo, "pull-requests", str(pr_number)) + ".diff"
        out = self._get(path)
        return out if isinstance(out, str) else ""

    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Return file content at ref (raw endpoint with at=ref)."""
        url = self._path(owner, repo, "raw", path.lstrip("/"))
        raw = self._get_raw_bytes(url, params={"at": ref})
        text = raw.decode("utf-8", errors="replace")
        return truncate_repo_content(text, max_bytes=MAX_REPO_FILE_BYTES)

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        """Return list of changed files by parsing the PR diff."""
        diff_text = self.get_pr_diff(owner, repo, pr_number)
        seen: set[str] = set()
        result: list[FileInfo] = []
        for hunk in parse_unified_diff(diff_text):
            if hunk.path and hunk.path not in seen:
                seen.add(hunk.path)
                result.append(FileInfo(path=hunk.path, status="modified", additions=0, deletions=0))
        return result

    def _anchor_path_for_diff(self, file_path: str) -> str:
        """Normalize path so it matches the PR diff (enables inline comments on the diff view).
        Strips dst:// and src:// prefixes so e.g. dst://src/main/java/foo.java -> src/main/java/foo.java."""
        p = (file_path or "").strip()
        for prefix in ("dst://", "src://"):
            if p.lower().startswith(prefix):
                p = p[len(prefix) :].lstrip("/")
                break
        return p.lstrip("/") or file_path or ""

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
            from_id = (from_ref.get("latestCommit") or {}).get("id") or from_ref.get("id")
            to_id = (to_ref.get("latestCommit") or {}).get("id") or to_ref.get("id")
            return (from_id, to_id)
        except Exception:
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
        of on the line. fromHash/toHash place the anchor in the correct diff."""
        if not comments:
            return
        path = self._path(owner, repo, "pull-requests", str(pr_number), "comments")
        from_hash, to_hash = self._get_pr_diff_refs(owner, repo, pr_number)
        if to_hash is None and head_sha:
            to_hash = head_sha
        for c in comments:
            anchor_path = self._anchor_path_for_diff(c.path)
            line_type = getattr(c, "line_type", None) or "ADDED"
            anchor: dict[str, Any] = {
                "path": anchor_path,
                "line": c.line,
                "lineType": line_type,
                "fileType": "TO",
            }
            if from_hash and to_hash:
                anchor["fromHash"] = from_hash
                anchor["toHash"] = to_hash
                anchor["diffType"] = "EFFECTIVE"
            payload = {"text": c.body, "anchor": anchor}
            self._post(path, payload)

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
            if not isinstance(data, dict):
                break
            values = data.get("values") or []
            if not isinstance(values, list):
                break
            for act in values:
                if not isinstance(act, dict) or act.get("action") != "COMMENTED":
                    continue
                c = act.get("comment")
                if not isinstance(c, dict):
                    continue
                anchor = c.get("anchor") or {}
                path_str = anchor.get("path") or ""
                line = int(anchor.get("line", 0) or 0)
                body = c.get("text") or ""
                result.append(
                    ReviewComment(
                        id=str(c.get("id", "")),
                        path=path_str,
                        line=line,
                        body=body,
                        resolved=bool(c.get("state") == "RESOLVED"),
                    )
                )
            if data.get("isLastPage", True) or len(values) == 0:
                break
            next_start = data.get("nextPageStart")
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
        except Exception:
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
