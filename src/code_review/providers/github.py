"""GitHub API provider (for local testing without Gitea)."""

import logging
import os
from typing import Any, Literal

from github.GithubException import GithubException

from code_review.formatters.comment import (
    infer_severity_from_comment_body,
    max_inferred_severity,
    render_suggestion_block,
)
from code_review.github_client import GitHubApiClient
from code_review.diff.utils import normalize_path
from code_review.providers.base import (
    BotAttributionIdentity,
    BotBlockingState,
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderInterface,
    ProviderCapabilities,
    ReviewComment,
    ReviewDecision,
    UnresolvedReviewItem,
    _log_pr_info_warning,
    _log_pr_commit_messages_warning,
    unified_diff_for_path,
)
from code_review.providers.bot_blocking_common import (
    blocking_state_from_token_and_github_style_review_list,
)
from code_review.providers.review_decision_common import github_style_pull_review_json
from code_review.providers.safety import MAX_REPO_FILE_BYTES, truncate_repo_content
from code_review.schemas.review_thread_dismissal import (
    ReviewThreadDismissalContext,
    ReviewThreadDismissalEntry,
)

logger = logging.getLogger(__name__)


class GitHubProvider(ProviderInterface):
    """GitHub API client for PR diff, file content, and review comments."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._github_client: GitHubApiClient | None = None

    def _client(self) -> GitHubApiClient:
        if self._github_client is None:
            self._github_client = GitHubApiClient(
                self._base_url,
                self._token,
                timeout=self._timeout,
            )
        return self._github_client

    @staticmethod
    def _sha_guard_passes(base_sha: str, head_sha: str) -> bool:
        base = (base_sha or "").strip()
        head = (head_sha or "").strip()
        return bool(base and head and base != head)

    def _get_diff(self, path: str) -> str:
        """GET with Accept application/vnd.github.v3.diff for unified diff."""
        return self._client().request_text(
            "GET",
            path,
            headers={"Accept": "application/vnd.github.v3.diff"},
        )

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        body = self._client().graphql_query(query, variables)
        if not isinstance(body, dict):
            raise RuntimeError("GitHub GraphQL: invalid JSON body")
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
        except (GithubException, RuntimeError) as e:
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

    def get_incremental_pr_diff(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> str:
        if not self._sha_guard_passes(base_sha, head_sha):
            return self.get_pr_diff(owner, repo, pr_number)
        return self._get_incremental_pr_diff(owner, repo, pr_number, base_sha, head_sha)

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
        except GithubException as e:
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
        content = self._client().get_repo(owner, repo).get_contents(path, ref=ref)
        if isinstance(content, list):
            raise ValueError(f"Unexpected response for {path} at {ref}")
        raw = content.decoded_content.decode("utf-8", errors="replace")
        return truncate_repo_content(raw, max_bytes=MAX_REPO_FILE_BYTES)

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        """Return list of changed files in the PR."""
        files = self._client().get_pull(owner, repo, pr_number).get_files()
        result: list[FileInfo] = []
        for item in files:
            result.append(self._github_file_info(item))
        return result

    @staticmethod
    def _github_pr_file_data(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return item
        return {
            "filename": str(getattr(item, "filename", "") or ""),
            "previous_filename": str(getattr(item, "previous_filename", "") or ""),
            "status": str(getattr(item, "status", "modified") or "modified"),
            "additions": int(getattr(item, "additions", 0) or 0),
            "deletions": int(getattr(item, "deletions", 0) or 0),
            "patch": str(getattr(item, "patch", "") or ""),
        }

    @classmethod
    def _github_file_info(cls, item: Any) -> FileInfo:
        data = cls._github_pr_file_data(item)
        return FileInfo(
            path=str(data.get("filename") or ""),
            status=str(data.get("status") or "modified"),
            additions=int(data.get("additions") or 0),
            deletions=int(data.get("deletions") or 0),
        )

    @staticmethod
    def _github_pr_file_matches_path(item: Any, wanted_path: str) -> bool:
        data = GitHubProvider._github_pr_file_data(item)
        filename = normalize_path(str(data.get("filename") or ""), strip_git_prefixes=False)
        previous = normalize_path(
            str(data.get("previous_filename") or ""),
            strip_git_prefixes=False,
        )
        return wanted_path in {filename, previous}

    @staticmethod
    def _github_single_file_diff_from_item(item: Any) -> str | None:
        data = GitHubProvider._github_pr_file_data(item)
        patch_text = str(data.get("patch") or "").strip()
        if not patch_text:
            return None
        old_path = str(data.get("previous_filename") or data.get("filename") or "")
        new_path = str(data.get("filename") or old_path)
        lines = [
            f"diff --git a/{old_path} b/{new_path}",
            f"--- a/{old_path}",
            f"+++ b/{new_path}",
            patch_text,
        ]
        return "\n".join(lines)

    def _github_diff_for_matching_pr_file(self, data: list[Any], wanted_path: str) -> str | None:
        for item in data:
            if not self._github_pr_file_matches_path(item, wanted_path):
                continue
            return self._github_single_file_diff_from_item(item)
        return None

    def get_pr_diff_for_file(self, owner: str, repo: str, pr_number: int, path: str) -> str:
        """Return a single-file diff using GitHub's per-file ``patch`` payload when available."""
        wanted_path = normalize_path(path, strip_git_prefixes=False)
        if not wanted_path:
            return ""
        data = list(self._client().get_pull(owner, repo, pr_number).get_files())
        if not data:
            return ""
        match = self._github_diff_for_matching_pr_file(data, wanted_path)
        if match is not None:
            return match
        if any(self._github_pr_file_matches_path(item, wanted_path) for item in data):
            return unified_diff_for_path(self.get_pr_diff(owner, repo, pr_number), path)
        return ""

    def get_incremental_pr_files(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> list[FileInfo]:
        if not self._sha_guard_passes(base_sha, head_sha):
            return self.get_pr_files(owner, repo, pr_number)
        return self._get_incremental_pr_files(owner, repo, pr_number, base_sha, head_sha)

    def _get_incremental_pr_files(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> list[FileInfo]:
        """Return files changed in the incremental compare range ``base_sha...head_sha``."""
        try:
            comparison = self._client().get_repo(owner, repo).compare(base_sha, head_sha)
        except GithubException as e:
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
        files = comparison.files or []
        return [self._github_file_info(item) for item in files]

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
        self._client().create_pull_review(
            owner,
            repo,
            pr_number,
            event=str(payload["event"]),
            body=str(payload["body"]),
            head_sha=head_sha,
            comments=review_comments,
        )

    def get_existing_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewComment]:
        """Return existing review comments. GitHub does not expose 'resolved' on list."""
        result: list[ReviewComment] = []
        for c in self._client().get_pull(owner, repo, pr_number).get_review_comments():
            result.append(
                ReviewComment(
                    id=str(getattr(c, "id", "") or ""),
                    path=str(getattr(c, "path", "") or ""),
                    line=int(getattr(c, "line", 0) or 0),
                    body=str(getattr(c, "body", "") or ""),
                    resolved=False,
                )
            )
        return result

    def _github_token_user_login_lower(self) -> str | None:
        try:
            login = str(getattr(self._client().get_authenticated_user(), "login", "") or "")
            login = login.strip().lower()
            return login or None
        except Exception as e:
            logger.warning("GitHub GET /user failed for bot blocking state: %s", e)
        return None

    def _github_list_pull_reviews(self, owner: str, repo: str, pr_number: int) -> list[Any] | None:
        """List PR reviews, or ``None`` if the listing failed (caller maps to ``UNKNOWN``)."""
        try:
            out: list[Any] = []
            for review in self._client().get_pull(owner, repo, pr_number).get_reviews():
                out.append(
                    {
                        "id": int(getattr(review, "id", 0) or 0),
                        "state": str(getattr(review, "state", "") or ""),
                        "user": {
                            "login": str(
                                getattr(getattr(review, "user", None), "login", "") or ""
                            )
                        },
                    }
                )
            return out
        except Exception as e:
            logger.warning(
                "GitHub list PR reviews failed owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return None

    def get_bot_blocking_state(self, owner: str, repo: str, pr_number: int) -> BotBlockingState:
        """Latest token-user PR review: ``CHANGES_REQUESTED`` → blocking."""
        return blocking_state_from_token_and_github_style_review_list(
            self._github_token_user_login_lower(),
            self._github_list_pull_reviews(owner, repo, pr_number),
        )

    def get_bot_attribution_identity(
        self, owner: str, repo: str, pr_number: int
    ) -> BotAttributionIdentity:
        # Try the API first to get both login and numeric id_str.
        # GET /user returns 403 for GitHub App installation tokens, so fall back
        # to SCM_GITHUB_APP_BOT_LOGIN when the call fails or returns nothing usable.
        try:
            user = self._client().get_authenticated_user()
            login = str(getattr(user, "login", "") or "").strip().lower()
            uid = str(getattr(user, "id", "") or "").strip()
            if login:
                return BotAttributionIdentity(login=login, id_str=uid)
        except Exception as e:
            logger.warning("GitHub get_bot_attribution_identity /user failed: %s", e)
        app_bot_login = os.environ.get("SCM_GITHUB_APP_BOT_LOGIN", "").strip()
        if app_bot_login:
            return BotAttributionIdentity(login=app_bot_login.lower())
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

    @staticmethod
    def _github_comment_database_id(comment: dict[str, Any]) -> int:
        try:
            return int(comment.get("databaseId") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _github_thread_id(node: dict[str, Any]) -> str:
        return str(node.get("id") or "")

    @staticmethod
    def _github_thread_comments_wrapper(node: dict[str, Any]) -> dict[str, Any] | None:
        comments = node.get("comments")
        return comments if isinstance(comments, dict) else None

    def _github_thread_contains_comment_id(
        self, comments: list[dict[str, Any]], want_id: int
    ) -> bool:
        return any(self._github_comment_database_id(comment) == want_id for comment in comments)

    def _github_dismissal_context_from_thread_node(
        self, node: dict[str, Any], want_id: int
    ) -> ReviewThreadDismissalContext | None:
        tid = self._github_thread_id(node)
        if not tid:
            return None
        expanded = self._github_expand_thread_comments(
            tid,
            self._github_thread_comments_wrapper(node),
            gate_mode=False,
        )
        if not self._github_thread_contains_comment_id(expanded, want_id):
            return None
        return self._github_build_dismissal_context_from_comment_nodes(tid, expanded)

    def _github_dismissal_context_in_thread_nodes(
        self, nodes: list[Any], want_id: int
    ) -> ReviewThreadDismissalContext | None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            hit = self._github_dismissal_context_from_thread_node(node, want_id)
            if hit is not None:
                return hit
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
        self._client().reply_to_review_comment(
            owner,
            repo,
            pr_number,
            rid,
            body,
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
        self._client().create_pull_review(
            owner,
            repo,
            pr_number,
            event=str(payload["event"]),
            body=str(payload["body"]),
            head_sha=head_sha,
        )

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post PR-level comment (GitHub: issues comments endpoint for PRs)."""
        self._client().get_issue(owner, repo, pr_number).create_comment(body)

    @staticmethod
    def _github_commit_message(item: Any) -> str:
        commit = getattr(item, "commit", None)
        message = getattr(commit, "message", None)
        if message is None:
            raw_data = getattr(item, "raw_data", None)
            if isinstance(raw_data, dict):
                commit_dict = raw_data.get("commit") if isinstance(raw_data.get("commit"), dict) else {}
                message = commit_dict.get("message") or raw_data.get("message")
        text = str(message or "").strip()
        return text

    def get_pr_commit_messages(self, owner: str, repo: str, pr_number: int) -> list[str]:
        """List commits on the PR (GitHub: GET /pulls/{id}/commits)."""
        try:
            commits = self._client().get_pull(owner, repo, pr_number).get_commits()
        except Exception as e:
            _log_pr_commit_messages_warning(logger, owner, repo, pr_number, e)
            return []
        out: list[str] = []
        for item in commits:
            msg = self._github_commit_message(item)
            if msg:
                out.append(msg)
        return out

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """Return PR title, labels, and description for skip-review and metadata."""
        try:
            pull = self._client().get_pull(owner, repo, pr_number)
            labels = [
                str(getattr(label, "name", "") or "").strip()
                for label in (getattr(pull, "labels", None) or [])
            ]
            head_sha = str(getattr(getattr(pull, "head", None), "sha", "") or "").strip()
            return PRInfo(
                title=str(getattr(pull, "title", "") or ""),
                labels=[label for label in labels if label],
                description=str(getattr(pull, "body", "") or ""),
                head_sha=head_sha,
            )
        except Exception as e:
            _log_pr_info_warning(logger, owner, repo, pr_number, e)
            return None

    def update_pr_description(
        self, owner: str, repo: str, pr_number: int, description: str, title: str | None = None
    ) -> None:
        """Update the PR body (and optionally title) via PATCH /repos/.../pulls/{number}."""
        pull = self._client().get_pull(owner, repo, pr_number)
        if title is None:
            pull.edit(body=description)
            return
        pull.edit(title=title, body=description)

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
