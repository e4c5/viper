"""GitHub API provider (for local testing without Gitea)."""

import base64
import logging
from typing import Any, Literal

import httpx

from code_review.providers.base import (
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    ProviderInterface,
    ReviewDecision,
    ReviewComment,
    UnresolvedReviewItem,
    _log_pr_commit_messages_warning,
    _log_pr_info_warning,
    commit_messages_from_commit_list,
    file_infos_from_pull_file_list,
    pr_info_from_api_dict,
)
from code_review.formatters.comment import (
    infer_severity_from_comment_body,
    max_inferred_severity,
    render_suggestion_block,
)
from code_review.providers.safety import truncate_repo_content

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
DEFAULT_BASE_URL = "https://api.github.com"
JSON_MEDIA_TYPE = "application/json"
logger = logging.getLogger(__name__)


class GitHubProvider(ProviderInterface):
    """GitHub API client for PR diff, file content, and review comments."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str = "", timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "Accept": "application/vnd.github+json",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(url, headers=self._headers(), params=params or {})
            r.raise_for_status()
            if r.headers.get("content-type", "").startswith(JSON_MEDIA_TYPE):
                return r.json()
            return r.text

    def _get_diff(self, path: str) -> str:
        """GET with Accept application/vnd.github.v3.diff for unified diff."""
        url = f"{self._base_url}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(
                url,
                headers={**self._headers(), "Accept": "application/vnd.github.v3.diff"},
            )
            r.raise_for_status()
            return r.text

    def _post(self, path: str, json: Any) -> Any:
        url = f"{self._base_url}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(url, headers=self._headers(), json=json)
            r.raise_for_status()
            return r.json() if r.content else None

    def _patch(self, path: str, json: Any) -> Any:
        url = f"{self._base_url}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            r = client.patch(url, headers=self._headers(), json=json)
            r.raise_for_status()
            return r.json() if r.content else None

    def _graphql_endpoint(self) -> str:
        u = self._base_url.rstrip("/")
        if "api.github.com" in u:
            return "https://api.github.com/graphql"
        if u.endswith("/api/v3"):
            return u[: -len("/api/v3")] + "/api/graphql"
        return f"{u}/api/graphql"

    def _graphql_headers(self) -> dict[str, str]:
        h = {"Content-Type": JSON_MEDIA_TYPE, "Accept": JSON_MEDIA_TYPE}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        url = self._graphql_endpoint()
        payload = {"query": query, "variables": variables}
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(url, headers=self._graphql_headers(), json=payload)
            r.raise_for_status()
            body = r.json()
        if not isinstance(body, dict):
            raise RuntimeError("GitHub GraphQL: invalid JSON body")
        if body.get("errors"):
            raise RuntimeError(f"GitHub GraphQL errors: {body['errors']}")
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    _REVIEW_THREADS_GQL = """
    query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          reviewThreads(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              isResolved
              isOutdated
              comments(first: 50) {
                nodes {
                  databaseId
                  body
                  path
                  line
                }
              }
            }
          }
        }
      }
    }
    """

    @staticmethod
    def _github_graphql_review_threads_container(data: dict[str, Any]) -> dict[str, Any] | None:
        repo_d = data.get("repository")
        if not isinstance(repo_d, dict):
            return None
        pr_d = repo_d.get("pullRequest")
        if not isinstance(pr_d, dict):
            return None
        rt = pr_d.get("reviewThreads")
        return rt if isinstance(rt, dict) else None

    @staticmethod
    def _github_thread_node_to_unresolved_item(node: dict[str, Any]) -> UnresolvedReviewItem | None:
        if node.get("isResolved") or node.get("isOutdated"):
            return None
        comments_wrap = node.get("comments") or {}
        cnodes = comments_wrap.get("nodes") if isinstance(comments_wrap, dict) else None
        if not isinstance(cnodes, list) or not cnodes:
            return None
        best_sev: Literal["high", "medium", "low", "nit", "unknown"] = "unknown"
        body_text = ""
        path_str = ""
        line_no = 0
        for c in cnodes:
            if not isinstance(c, dict):
                continue
            raw_body = (c.get("body") or "").strip()
            if not raw_body:
                continue
            sev = infer_severity_from_comment_body(raw_body)
            best_sev = max_inferred_severity(best_sev, sev)
            if not body_text:
                body_text = c.get("body") or ""
                path_str = str(c.get("path") or "")
                line_no = int(c.get("line") or 0)
        if not body_text:
            return None
        tid = str(node.get("id") or "")
        return UnresolvedReviewItem(
            stable_id=f"github:thread:{tid}",
            thread_id=tid,
            kind="discussion_thread",
            path=path_str,
            line=line_no,
            body=body_text,
            inferred_severity=best_sev,
        )

    @staticmethod
    def _github_review_threads_advance_cursor(
        rt: dict[str, Any],
        seen_end_cursors: set[str],
        owner: str,
        repo: str,
        pr_number: int,
    ) -> str | None:
        """Return next GraphQL ``after`` cursor, or None when pagination should stop."""
        page = rt.get("pageInfo") or {}
        if not isinstance(page, dict) or not page.get("hasNextPage"):
            return None
        next_cursor = page.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            return None
        if next_cursor in seen_end_cursors:
            logger.warning(
                "GitHub GraphQL reviewThreads pagination loop detected (repeated endCursor) "
                "owner=%s repo=%s pr=%s",
                owner,
                repo,
                pr_number,
            )
            return None
        seen_end_cursors.add(next_cursor)
        return next_cursor

    def _unresolved_review_threads_graphql(
        self, owner: str, repo: str, pr_number: int
    ) -> list[UnresolvedReviewItem]:
        """List unresolved, non-outdated review threads via GitHub GraphQL."""
        out: list[UnresolvedReviewItem] = []
        cursor: str | None = None
        seen_end_cursors: set[str] = set()
        max_pages = 500
        for _ in range(max_pages):
            variables: dict[str, Any] = {
                "owner": owner,
                "name": repo,
                "number": int(pr_number),
                "cursor": cursor,
            }
            data = self._graphql(self._REVIEW_THREADS_GQL, variables)
            rt = self._github_graphql_review_threads_container(data)
            if rt is None:
                break
            for node in rt.get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                item = self._github_thread_node_to_unresolved_item(node)
                if item is not None:
                    out.append(item)
            cursor = self._github_review_threads_advance_cursor(
                rt, seen_end_cursors, owner, repo, pr_number
            )
            if cursor is None:
                break
        else:
            logger.warning(
                "GitHub GraphQL reviewThreads pagination exceeded max_pages=%s owner=%s repo=%s pr=%s",
                max_pages,
                owner,
                repo,
                pr_number,
            )
        return out

    def get_unresolved_review_items_for_quality_gate(
        self, owner: str, repo: str, pr_number: int
    ) -> list[UnresolvedReviewItem]:
        """Use GraphQL review threads (resolved / outdated). On GraphQL failure, return []."""
        try:
            return self._unresolved_review_threads_graphql(owner, repo, pr_number)
        except Exception as e:
            logger.warning(
                "GitHub GraphQL reviewThreads failed owner=%s repo=%s pr=%s: %s; "
                "skipping pre-existing unresolved aggregation (REST comments lack resolution state).",
                owner,
                repo,
                pr_number,
                e,
            )
            return []

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff for the PR (Accept: application/vnd.github.v3.diff)."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        return self._get_diff(path)

    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Return file content at ref; truncated if over max size."""
        api_path = f"/repos/{owner}/{repo}/contents/{path}"
        resp = self._get(api_path, params={"ref": ref})
        if isinstance(resp, dict) and "content" in resp:
            raw = base64.b64decode(resp["content"]).decode("utf-8", errors="replace")
            return truncate_repo_content(raw, max_bytes=MAX_REPO_FILE_BYTES)
        raise ValueError(f"Unexpected response for {path} at {ref}")

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        """Return list of changed files in the PR."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
        data = self._get(path, params={"per_page": 100})
        return file_infos_from_pull_file_list(data) if isinstance(data, list) else []

    def post_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        comments: list[InlineComment],
        head_sha: str = "",
    ) -> None:
        """Post inline review comments via Create a review (event COMMENT, comments array)."""
        if not comments:
            return
        review_comments = [
            {
                "path": c.path,
                "side": "RIGHT",
                "body": render_suggestion_block(c.body, c.suggested_patch),
                **(
                    {"start_line": c.line, "start_side": "RIGHT", "line": c.end_line}
                    if (c.end_line is not None and c.end_line != c.line)
                    else {"line": c.line}
                ),
            }
            for c in comments
        ]
        payload: dict[str, Any] = {
            "event": "COMMENT",
            "body": "Code review comments",
            "comments": review_comments,
        }
        if head_sha:
            payload["commit_id"] = head_sha
        self._post(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", payload)

    def get_existing_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewComment]:
        """Return existing review comments. GitHub does not expose 'resolved' on list."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        data = self._get(path, params={"per_page": 100})
        if not isinstance(data, list):
            return []
        result: list[ReviewComment] = []
        for c in data:
            result.append(
                ReviewComment(
                    id=str(c.get("id", "")),
                    path=c.get("path", ""),
                    line=int(c.get("line", 0) or 0),
                    body=c.get("body", ""),
                    resolved=False,
                )
            )
        return result

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
        """Submit a PR-level review decision on GitHub."""
        payload: dict[str, Any] = {
            "event": decision,
            "body": body or "Automated review decision by Viper.",
        }
        if head_sha:
            payload["commit_id"] = head_sha
        self._post(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", payload)

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post PR-level comment (GitHub: issues comments endpoint for PRs)."""
        self._post(f"/repos/{owner}/{repo}/issues/{pr_number}/comments", {"body": body})

    def get_pr_commit_messages(self, owner: str, repo: str, pr_number: int) -> list[str]:
        """List commits on the PR (GitHub: GET /pulls/{id}/commits)."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/commits"
        try:
            data = self._get(path, params={"per_page": 100})
        except Exception as e:
            _log_pr_commit_messages_warning(logger, owner, repo, pr_number, e)
            return []
        return commit_messages_from_commit_list(data)

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """Return PR title, labels, and description for skip-review and metadata."""
        try:
            data = self._get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
            return pr_info_from_api_dict(data, "body") if isinstance(data, dict) else None
        except Exception as e:
            _log_pr_info_warning(logger, owner, repo, pr_number, e)
            return None

    def update_pr_description(
        self, owner: str, repo: str, pr_number: int, description: str, title: str | None = None
    ) -> None:
        """Update the PR body (and optionally title) via PATCH /repos/.../pulls/{number}."""
        payload: dict[str, str] = {"body": description}
        if title is not None:
            payload["title"] = title
        self._patch(f"/repos/{owner}/{repo}/pulls/{pr_number}", payload)

    def capabilities(self) -> ProviderCapabilities:
        """GitHub supports suggestion blocks; resolved is per-conversation, not per-comment."""
        return ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=True,
            supports_multiline_suggestions=True,
            supports_review_decisions=True,
        )
