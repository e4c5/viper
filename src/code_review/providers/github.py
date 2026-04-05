"""GitHub API provider (for local testing without Gitea)."""

import base64
import json
import logging
import os
from typing import Any, Literal

import httpx

from code_review.formatters.comment import (
    infer_severity_from_comment_body,
    max_inferred_severity,
    render_suggestion_block,
)
from code_review.diff.utils import normalize_path
from code_review.providers.base import (
    BotAttributionIdentity,
    BotBlockingState,
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    ReviewComment,
    ReviewDecision,
    UnresolvedReviewItem,
    _log_pr_commit_messages_warning,
    commit_messages_from_commit_list,
    file_infos_from_pull_file_list,
)
from code_review.providers.bot_blocking_common import (
    blocking_state_from_token_and_github_style_review_list,
)
from code_review.providers.http_base import HttpXProvider
from code_review.providers.review_decision_common import github_style_pull_review_json
from code_review.providers.safety import MAX_REPO_FILE_BYTES, truncate_repo_content
from code_review.schemas.review_thread_dismissal import (
    ReviewThreadDismissalContext,
    ReviewThreadDismissalEntry,
)

DEFAULT_BASE_URL = "https://api.github.com"
JSON_MEDIA_TYPE = "application/json"
logger = logging.getLogger(__name__)


class GitHubProvider(HttpXProvider):
    """GitHub API client for PR diff, file content, and review comments."""

    _httpx_module = httpx

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _default_headers(self) -> dict[str, str]:
        return {"Accept": "application/vnd.github+json"}

    def _get_diff(self, path: str) -> str:
        """GET with Accept application/vnd.github.v3.diff for unified diff."""
        return self._get_text(path, headers={"Accept": "application/vnd.github.v3.diff"})

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
                pageInfo { hasNextPage endCursor }
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

    _DISMISSAL_THREADS_GQL = """
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
                pageInfo { hasNextPage endCursor }
                nodes {
                  databaseId
                  body
                  path
                  line
                  createdAt
                  author { login }
                }
              }
            }
          }
        }
      }
    }
    """

    _RESOLVE_REVIEW_THREAD_GQL = """
    mutation($threadId: ID!) {
      resolveReviewThread(input: {threadId: $threadId}) {
        thread {
          id
          isResolved
        }
      }
    }
    """

    _THREAD_COMMENTS_PAGE_GQL = """
    query($threadId: ID!, $commentCursor: String) {
      node(id: $threadId) {
        ... on PullRequestReviewThread {
          comments(first: 100, after: $commentCursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              databaseId
              body
              path
              line
              createdAt
              author { login }
            }
          }
        }
      }
    }
    """

    _THREAD_COMMENTS_PAGE_GATE_GQL = """
    query($threadId: ID!, $commentCursor: String) {
      node(id: $threadId) {
        ... on PullRequestReviewThread {
          comments(first: 100, after: $commentCursor) {
            pageInfo { hasNextPage endCursor }
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
    def _github_aggregate_thread_comments(
        cnodes: list[Any],
    ) -> tuple[str, str, int, Literal["high", "medium", "low", "nit", "unknown"]] | None:
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
        return body_text, path_str, line_no, best_sev

    _THREAD_COMMENTS_FETCH_FIRST: str = "__first_page__"

    @staticmethod
    def _github_append_thread_comment_dicts_from_nodes(
        nodes: Any, out: list[dict[str, Any]]
    ) -> None:
        if not isinstance(nodes, list):
            return
        for c in nodes:
            if isinstance(c, dict):
                out.append(c)

    def _github_thread_comments_pagination_start(
        self,
        initial_comments: dict[str, Any] | None,
        out: list[dict[str, Any]],
    ) -> tuple[bool, str | None]:
        """(True, _) => pagination finished (return ``out``); (False, cursor) => fetch more."""
        if not isinstance(initial_comments, dict):
            return False, self._THREAD_COMMENTS_FETCH_FIRST
        self._github_append_thread_comment_dicts_from_nodes(
            initial_comments.get("nodes"), out
        )
        page = initial_comments.get("pageInfo") or {}
        if not (isinstance(page, dict) and page.get("hasNextPage")):
            return True, None
        ec = page.get("endCursor")
        cur = ec if isinstance(ec, str) and ec else None
        return False, cur

    def _github_thread_comments_merge_next_page(
        self,
        gql: str,
        thread_id: str,
        fetch_cursor: str | None,
        out: list[dict[str, Any]],
        seen_end: set[str],
    ) -> str | None:
        """Run one GraphQL page fetch; return next cursor or None to stop."""
        if fetch_cursor is None:
            return None
        variables: dict[str, Any] = {"threadId": thread_id}
        if fetch_cursor == self._THREAD_COMMENTS_FETCH_FIRST:
            variables["commentCursor"] = None
        else:
            variables["commentCursor"] = fetch_cursor
        data = self._graphql(gql, variables)
        node = data.get("node") if isinstance(data, dict) else None
        if not isinstance(node, dict):
            return None
        conn = node.get("comments") or {}
        if not isinstance(conn, dict):
            return None
        self._github_append_thread_comment_dicts_from_nodes(conn.get("nodes"), out)
        page = conn.get("pageInfo") or {}
        if not isinstance(page, dict) or not page.get("hasNextPage"):
            return None
        ec = page.get("endCursor")
        if not isinstance(ec, str) or not ec:
            return None
        if ec in seen_end:
            return None
        seen_end.add(ec)
        return ec

    def _github_expand_thread_comments(
        self,
        thread_id: str,
        initial_comments: dict[str, Any] | None,
        *,
        gate_mode: bool = False,
    ) -> list[dict[str, Any]]:
        """All comments on a review thread (paginates past ``comments(first: N)`` list slices)."""
        if not (thread_id or "").strip():
            return []
        gql = (
            self._THREAD_COMMENTS_PAGE_GATE_GQL
            if gate_mode
            else self._THREAD_COMMENTS_PAGE_GQL
        )
        out: list[dict[str, Any]] = []
        done, fetch_cursor = self._github_thread_comments_pagination_start(
            initial_comments, out
        )
        if done:
            return out
        seen_end: set[str] = set()
        for _ in range(500):
            fetch_cursor = self._github_thread_comments_merge_next_page(
                gql, thread_id, fetch_cursor, out, seen_end
            )
            if fetch_cursor is None:
                break
        return out

    def _github_thread_node_to_unresolved_item(
        self, node: dict[str, Any]
    ) -> UnresolvedReviewItem | None:
        if node.get("isResolved") or node.get("isOutdated"):
            return None
        tid = str(node.get("id") or "")
        comments_wrap = node.get("comments") if isinstance(node.get("comments"), dict) else None
        cnodes = self._github_expand_thread_comments(tid, comments_wrap, gate_mode=True)
        if not cnodes:
            return None
        agg = GitHubProvider._github_aggregate_thread_comments(cnodes)
        if agg is None:
            return None
        body_text, path_str, line_no, best_sev = agg
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
                "GitHub GraphQL reviewThreads pagination exceeded max_pages=%s "
                "owner=%s repo=%s pr=%s",
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
        except (httpx.HTTPError, json.JSONDecodeError, RuntimeError) as e:
            logger.warning(
                "GitHub GraphQL reviewThreads failed owner=%s repo=%s pr=%s: %s; "
                "skipping unresolved aggregation (REST comments lack resolution state).",
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

    def _get_incremental_pr_diff(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> str:
        """Return unified diff for the incremental compare range ``base_sha...head_sha``."""
        path = f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}"
        try:
            return self._get_diff(path)
        except httpx.HTTPError as e:
            logger.warning(
                "GitHub incremental compare diff failed owner=%s repo=%s pr=%s "
                "base=%s head=%s: %s; falling back to full PR diff",
                owner,
                repo,
                pr_number,
                base_sha,
                head_sha,
                e,
            )
            return self.get_pr_diff(owner, repo, pr_number)

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

    @staticmethod
    def _github_pr_file_matches_path(item: dict[str, Any], wanted_path: str) -> bool:
        filename = normalize_path(str(item.get("filename") or ""), strip_git_prefixes=False)
        previous = normalize_path(
            str(item.get("previous_filename") or ""),
            strip_git_prefixes=False,
        )
        return wanted_path in {filename, previous}

    @staticmethod
    def _github_single_file_diff_from_item(item: dict[str, Any]) -> str | None:
        patch_text = str(item.get("patch") or "").strip()
        if not patch_text:
            return None
        old_path = str(item.get("previous_filename") or item.get("filename") or "")
        new_path = str(item.get("filename") or old_path)
        lines = [
            f"diff --git a/{old_path} b/{new_path}",
            f"--- a/{old_path}",
            f"+++ b/{new_path}",
            patch_text,
        ]
        return "\n".join(lines)

    def _github_diff_for_matching_pr_file(
        self, data: list[Any], wanted_path: str
    ) -> str | None:
        for item in data:
            if not isinstance(item, dict):
                continue
            if not self._github_pr_file_matches_path(item, wanted_path):
                continue
            return self._github_single_file_diff_from_item(item)
        return None

    def get_pr_diff_for_file(self, owner: str, repo: str, pr_number: int, path: str) -> str:
        """Return a single-file diff using GitHub's per-file ``patch`` payload when available."""
        wanted_path = normalize_path(path, strip_git_prefixes=False)
        if not wanted_path:
            return ""
        api_path = f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
        page = 1
        for _ in range(100):
            data = self._get(api_path, params={"per_page": 100, "page": page})
            if not isinstance(data, list) or not data:
                return ""
            match = self._github_diff_for_matching_pr_file(data, wanted_path)
            if match is not None:
                return match
            if any(
                isinstance(item, dict) and self._github_pr_file_matches_path(item, wanted_path)
                for item in data
            ):
                return super().get_pr_diff_for_file(owner, repo, pr_number, path)
            if len(data) < 100:
                return ""
            page += 1
        return ""

    def _get_incremental_pr_files(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> list[FileInfo]:
        """Return files changed in the incremental compare range ``base_sha...head_sha``."""
        path = f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}"
        try:
            data = self._get(path, params={"per_page": 100})
        except httpx.HTTPError as e:
            logger.warning(
                "GitHub incremental compare files failed owner=%s repo=%s pr=%s "
                "base=%s head=%s: %s; falling back to full PR files",
                owner,
                repo,
                pr_number,
                base_sha,
                head_sha,
                e,
            )
            return self.get_pr_files(owner, repo, pr_number)
        if not isinstance(data, dict):
            return []
        return file_infos_from_pull_file_list(data.get("files") or [])

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

    def _github_token_user_login_lower(self) -> str | None:
        try:
            data = self._get("/user")
            if isinstance(data, dict):
                login = str(data.get("login") or "").strip().lower()
                return login or None
        except Exception as e:
            logger.warning("GitHub GET /user failed for bot blocking state: %s", e)
        return None

    def _github_list_pull_reviews(self, owner: str, repo: str, pr_number: int) -> list[Any] | None:
        """List PR reviews, or ``None`` if the listing failed (caller maps to ``UNKNOWN``)."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        out: list[Any] = []
        page = 1
        for _ in range(50):
            try:
                data = self._get(path, params={"per_page": 100, "page": page})
            except Exception as e:
                logger.warning(
                    "GitHub list PR reviews failed owner=%s repo=%s pr=%s: %s",
                    owner,
                    repo,
                    pr_number,
                    e,
                )
                return None
            if not isinstance(data, list):
                logger.warning(
                    "GitHub list PR reviews unexpected JSON owner=%s repo=%s pr=%s page=%s",
                    owner,
                    repo,
                    pr_number,
                    page,
                )
                return None
            if not data:
                break
            out.extend(data)
            if len(data) < 100:
                break
            page += 1
        return out

    def get_bot_blocking_state(self, owner: str, repo: str, pr_number: int) -> BotBlockingState:
        """Latest token-user PR review: ``CHANGES_REQUESTED`` → blocking."""
        return blocking_state_from_token_and_github_style_review_list(
            self._github_token_user_login_lower(),
            self._github_list_pull_reviews(owner, repo, pr_number),
        )

    def get_bot_attribution_identity(
        self, owner: str, repo: str, pr_number: int
    ) -> BotAttributionIdentity:
        # When running via a GitHub App, the installation token cannot call GET /user
        # (returns 403). The app bot login is injected via SCM_GITHUB_APP_BOT_LOGIN.
        app_bot_login = os.environ.get("SCM_GITHUB_APP_BOT_LOGIN", "").strip()
        if app_bot_login:
            return BotAttributionIdentity(login=app_bot_login.lower())
        try:
            data = self._get("/user")
            if isinstance(data, dict):
                login = str(data.get("login") or "").strip().lower()
                uid = str(data.get("id") or "").strip()
                return BotAttributionIdentity(login=login, id_str=uid)
        except Exception as e:
            logger.warning("GitHub get_bot_attribution_identity failed: %s", e)
        return BotAttributionIdentity()

    def _github_build_dismissal_context_from_comment_nodes(
        self, thread_graphql_id: str, cnodes: list[dict[str, Any]]
    ) -> ReviewThreadDismissalContext | None:
        if not thread_graphql_id or not cnodes:
            return None
        entries = [
            self._github_dismissal_entry_from_comment_node(comment)
            for comment in cnodes
            if isinstance(comment, dict)
        ]
        if len(entries) < 2:
            return None
        path, line = self._github_thread_anchor_from_comment_nodes(cnodes)
        return ReviewThreadDismissalContext(
            gate_exclusion_stable_id=f"github:thread:{thread_graphql_id}",
            thread_id=thread_graphql_id,
            path=path,
            line=line,
            entries=entries,
        )

    @staticmethod
    def _github_dismissal_entry_from_comment_node(
        comment: dict[str, Any]
    ) -> ReviewThreadDismissalEntry:
        author = comment.get("author") if isinstance(comment.get("author"), dict) else {}
        return ReviewThreadDismissalEntry(
            comment_id=str(comment.get("databaseId") or ""),
            author_login=str(author.get("login") or ""),
            body=str(comment.get("body") or ""),
            created_at=str(comment.get("createdAt") or ""),
        )

    @staticmethod
    def _github_thread_anchor_from_comment_nodes(cnodes: list[dict[str, Any]]) -> tuple[str, int]:
        path = ""
        line = 0
        for comment in cnodes:
            if not isinstance(comment, dict):
                continue
            if not path:
                path = str(comment.get("path") or "")
            if not line:
                line = GitHubProvider._github_comment_line(comment)
            if path and line:
                break
        return path, line

    @staticmethod
    def _github_comment_line(comment: dict[str, Any]) -> int:
        try:
            return int(comment.get("line") or 0)
        except (TypeError, ValueError):
            return 0

    def _github_dismissal_context_in_thread_nodes(
        self, nodes: list[Any], want_id: int
    ) -> ReviewThreadDismissalContext | None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            tid = str(node.get("id") or "")
            if not tid:
                continue
            cw = node.get("comments") if isinstance(node.get("comments"), dict) else None
            expanded = self._github_expand_thread_comments(tid, cw, gate_mode=False)
            for c in expanded:
                if not isinstance(c, dict):
                    continue
                if int(c.get("databaseId") or 0) == want_id:
                    return self._github_build_dismissal_context_from_comment_nodes(
                        tid, expanded
                    )
        return None

    def get_review_thread_dismissal_context(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        triggered_comment_id: str,
    ) -> ReviewThreadDismissalContext | None:
        """Find the review thread containing the given comment ``databaseId``."""
        raw = (triggered_comment_id or "").strip()
        if not raw:
            return None
        try:
            want_id = int(raw)
        except ValueError:
            return None
        cursor: str | None = None
        seen_end_cursors: set[str] = set()
        max_pages = 500
        try:
            for _ in range(max_pages):
                variables: dict[str, Any] = {
                    "owner": owner,
                    "name": repo,
                    "number": int(pr_number),
                    "cursor": cursor,
                }
                data = self._graphql(self._DISMISSAL_THREADS_GQL, variables)
                rt = self._github_graphql_review_threads_container(data)
                if rt is None:
                    break
                hit = self._github_dismissal_context_in_thread_nodes(
                    rt.get("nodes") or [], want_id
                )
                if hit is not None:
                    return hit
                cursor = self._github_review_threads_advance_cursor(
                    rt, seen_end_cursors, owner, repo, pr_number
                )
                if cursor is None:
                    break
        except Exception as e:
            logger.warning(
                "GitHub get_review_thread_dismissal_context failed owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return None
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
            rid = int((reply_to_comment_id or "").strip())
        except ValueError as e:
            raise ValueError("GitHub reply_to_comment_id must be numeric") from e
        self._post(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            {"body": body, "in_reply_to": rid},
        )

    def resolve_review_thread(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        thread_context: ReviewThreadDismissalContext,
        triggered_comment_id: str,
    ) -> None:
        thread_id = (thread_context.thread_id or "").strip()
        if not thread_id:
            ctx = self.get_review_thread_dismissal_context(
                owner,
                repo,
                pr_number,
                triggered_comment_id,
            )
            thread_id = (ctx.thread_id or "").strip() if ctx is not None else ""
        if not thread_id:
            raise ValueError("GitHub review thread id is required to resolve the thread")
        self._graphql(self._RESOLVE_REVIEW_THREAD_GQL, {"threadId": thread_id})

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
        payload = github_style_pull_review_json(decision, body, head_sha)
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
        return self._get_pr_info_from_path(
            owner,
            repo,
            pr_number,
            path=f"/repos/{owner}/{repo}/pulls/{pr_number}",
            logger=logger,
        )

    def update_pr_description(
        self, owner: str, repo: str, pr_number: int, description: str, title: str | None = None
    ) -> None:
        """Update the PR body (and optionally title) via PATCH /repos/.../pulls/{number}."""
        self._patch_pr_description(
            path=f"/repos/{owner}/{repo}/pulls/{pr_number}",
            description=description,
            title=title,
        )

    def capabilities(self) -> ProviderCapabilities:
        """GitHub supports suggestion blocks; resolved is per-conversation, not per-comment."""
        return ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=True,
            supports_multiline_suggestions=True,
            supports_review_decisions=True,
            supports_bot_blocking_state_query=True,
            supports_bot_attribution_identity_query=True,
            supports_review_thread_dismissal_context=True,
            supports_lightweight_pr_diff_for_file=True,
            supports_review_thread_reply=True,
            supports_review_thread_resolution=True,
        )
