"""Gitea API provider."""

import logging
import time
from typing import Any

import httpx

from code_review.diff.position import get_diff_hunk_for_line
from code_review.formatters.comment import render_suggestion_block
from code_review.providers.base import (
    BotAttributionIdentity,
    BotBlockingState,
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    RateLimitError,
    ReviewComment,
    ReviewDecision,
    _log_pr_commit_messages_warning,
    commit_messages_from_commit_list,
    file_infos_from_pull_file_list,
)
from code_review.providers.bot_blocking_common import (
    blocking_state_from_token_and_github_style_review_list,
)
from code_review.providers.http_base import HttpXProvider
from code_review.providers.review_decision_common import github_style_pull_review_json
from code_review.providers.safety import truncate_repo_content

logger = logging.getLogger(__name__)
_COMPARE_FALLBACK_STATUSES = {404, 405, 422}


class GiteaProvider(HttpXProvider):
    """Gitea API client for PR diff, file content, and review comments."""

    _httpx_module = httpx

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"token {self._token}"}

    def _default_headers(self) -> dict[str, str]:
        return {"Accept": "application/json"}

    def _api_prefix(self) -> str:
        return "/api/v1"

    _RETRY_STATUSES = (502, 503, 504)
    _RETRY_DELAY_SECONDS = 1.0

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_retries: int = 1,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform request with one retry on transient server errors (502, 503, 504).

        Rate limit errors (429) are not retried; a RateLimitError is raised
        immediately so callers can skip to the next task rather than making
        the rate limit situation worse.
        """
        request_headers = self._headers()
        if headers:
            request_headers = {**request_headers, **headers}
        with self._httpx_module.Client(timeout=self._timeout) as client:
            r = client.request(method, url, headers=request_headers, **kwargs)
            if r.status_code == 429:
                raise RateLimitError(f"Rate limit exceeded (HTTP 429) for {method} {url}: {r.text}")
            if r.status_code in self._RETRY_STATUSES and max_retries > 0:
                time.sleep(self._RETRY_DELAY_SECONDS)
                r = client.request(method, url, headers=request_headers, **kwargs)
            return r

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        r = self._request_with_retry(
            method,
            self._build_url(path),
            params=params or None,
            json=json,
            headers=headers,
        )
        r.raise_for_status()
        return r

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff for the PR."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}.diff"
        return self._get_text(path)

    def _get_incremental_pr_diff(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> str:
        """Return unified diff for the incremental compare range ``base_sha...head_sha``."""
        path = f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}.diff"
        try:
            return self._get_text(path)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code not in _COMPARE_FALLBACK_STATUSES:
                raise
            logger.warning(
                "Gitea incremental compare diff unsupported/invalid owner=%s repo=%s pr=%s "
                "base=%s head=%s status=%s; falling back to full PR diff",
                owner,
                repo,
                pr_number,
                base_sha,
                head_sha,
                status_code,
            )
            return self.get_pr_diff(owner, repo, pr_number)

    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Return file content at ref; truncated with delimiter if over max size."""
        import base64
        import binascii

        api_path = f"/repos/{owner}/{repo}/contents/{path}"
        resp = self._get(api_path, params={"ref": ref})
        if isinstance(resp, dict) and "content" in resp:
            try:
                decoded = base64.b64decode(resp["content"])
            except (binascii.Error, TypeError) as exc:  # malformed or non-base64 content
                raise ValueError(f"Invalid base64 content for {path} at {ref}") from exc
            raw = decoded.decode("utf-8", errors="replace")
            return truncate_repo_content(raw)
        raise ValueError(f"Unexpected response for {path} at {ref}")

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        """Return list of changed files in the PR."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
        data = self._get(path)
        return file_infos_from_pull_file_list(data) if isinstance(data, list) else []

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
            data = self._get(path)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code not in _COMPARE_FALLBACK_STATUSES:
                raise
            logger.warning(
                "Gitea incremental compare files unsupported/invalid owner=%s repo=%s pr=%s "
                "base=%s head=%s status=%s; falling back to full PR files",
                owner,
                repo,
                pr_number,
                base_sha,
                head_sha,
                status_code,
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
        """Post inline review comments. Convert internal InlineComment to Gitea API payload.

        Gitea shows comments in the diff (Files changed) when each comment has valid path,
        new_position (line in new file), and commit_id. We fetch the PR diff and include
        diff_hunk per comment when possible so the UI can pin the comment to the diff section.
        """
        if not comments:
            return
        diff_text: str | None = None
        try:
            diff_text = self.get_pr_diff(owner, repo, pr_number)
        except Exception:
            pass
        review_comments: list[dict[str, Any]] = []
        for c in comments:
            path_norm = (c.path or "").lstrip("/")
            if not path_norm:
                path_norm = c.path or ""
            item: dict[str, Any] = {
                "path": path_norm,
                "body": render_suggestion_block(c.body, c.suggested_patch),
                "old_position": 0,
                "new_position": int(c.line),
            }
            if diff_text:
                hunk = get_diff_hunk_for_line(diff_text, c.path, c.line)
                if hunk:
                    item["diff_hunk"] = hunk
            review_comments.append(item)
        payload: dict[str, Any] = {
            "body": "Code review comments",
            "event": "COMMENT",
            "comments": review_comments,
        }
        if head_sha:
            payload["commit_id"] = head_sha
        self._post(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", payload)

    def get_existing_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewComment]:
        """Return existing review comments. Gitea may not expose 'resolved' via API."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        # Older Gitea versions (e.g. 1.21.x) do not expose this endpoint and return 404.
        # Treat 404 as "no existing review comments" instead of failing the whole run.
        try:
            data = self._get(path)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return []
            raise
        if not isinstance(data, list):
            return []
        result: list[ReviewComment] = []
        for c in data:
            # Gitea PR comments: id, path, line, body; resolved status may be absent
            result.append(
                ReviewComment(
                    id=str(c.get("id", "")),
                    path=c.get("path", ""),
                    line=int(c.get("line", 0) or 0),
                    body=c.get("body", ""),
                    resolved=bool(c.get("resolved", False)),
                )
            )
        return result

    def _gitea_token_user_login_lower(self) -> str | None:
        try:
            data = self._get("/user")
            if isinstance(data, dict):
                login = str(data.get("login") or "").strip().lower()
                return login or None
        except Exception as e:
            logger.warning("Gitea GET /user failed for bot blocking state: %s", e)
        return None

    def _gitea_list_pull_reviews(self, owner: str, repo: str, pr_number: int) -> list[Any] | None:
        """Return reviews, ``None`` if the endpoint is missing (cannot infer state)."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        out: list[Any] = []
        page = 1
        try:
            for _ in range(50):
                data = self._get(path, params={"limit": 100, "page": page})
                if not isinstance(data, list) or not data:
                    break
                out.extend(data)
                if len(data) < 100:
                    break
                page += 1
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (404, 405, 501):
                return None
            logger.warning(
                "Gitea list PR reviews failed owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                exc,
            )
            return None
        except Exception as e:
            logger.warning(
                "Gitea list PR reviews failed owner=%s repo=%s pr=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return None
        return out

    def get_bot_blocking_state(self, owner: str, repo: str, pr_number: int) -> BotBlockingState:
        """Latest token-user pull review; Gitea aligns with GitHub-style review states."""
        return blocking_state_from_token_and_github_style_review_list(
            self._gitea_token_user_login_lower(),
            self._gitea_list_pull_reviews(owner, repo, pr_number),
        )

    def get_bot_attribution_identity(
        self, owner: str, repo: str, pr_number: int
    ) -> BotAttributionIdentity:
        try:
            data = self._get("/user")
            if isinstance(data, dict):
                login = str(data.get("login") or "").strip().lower()
                uid = str(data.get("id") or "").strip()
                return BotAttributionIdentity(login=login, id_str=uid)
        except Exception as e:
            logger.warning("Gitea get_bot_attribution_identity failed: %s", e)
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
        """Submit a PR-level review decision on Gitea."""
        payload = github_style_pull_review_json(decision, body, head_sha)
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        try:
            self._post(path, payload)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code in (404, 405, 501):
                logger.warning(
                    "Gitea PR review decision not supported or rejected (HTTP %s) "
                    "owner=%s repo=%s pr=%s",
                    code,
                    owner,
                    repo,
                    pr_number,
                )
                return
            raise

    def resolve_comment(self, owner: str, repo: str, comment_id: str) -> None:
        """Mark comment as resolved. Gitea does not support updating PR review comments; no-op."""
        try:
            self._patch(
                f"/repos/{owner}/{repo}/pulls/comments/{comment_id}",
                {"resolved": True},
            )
        except httpx.HTTPStatusError:
            # Gitea API does not support PATCH on PR review comments (typically 404/405)
            # No-op for runtime safety if called despite capabilities() returning False
            pass

    def unresolve_comment(self, owner: str, repo: str, comment_id: str) -> None:
        """Mark comment as unresolved. Gitea does not support updating PR review comments; no-op."""
        try:
            self._patch(
                f"/repos/{owner}/{repo}/pulls/comments/{comment_id}",
                {"resolved": False},
            )
        except httpx.HTTPStatusError:
            # Gitea API does not support PATCH on PR review comments (typically 404/405)
            # No-op for runtime safety if called despite capabilities() returning False
            pass

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post PR-level comment. In Gitea, PRs are issues; use issues comments endpoint."""
        self._post(
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            {"body": body},
        )

    def get_pr_commit_messages(self, owner: str, repo: str, pr_number: int) -> list[str]:
        """List commits on the PR (GET /repos/{owner}/{repo}/pulls/{index}/commits)."""
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/commits"
        out: list[str] = []
        page = 1
        limit = 50
        max_pages = 500
        for _ in range(max_pages):
            try:
                data = self._get(path, params={"limit": limit, "page": page})
            except Exception as e:
                _log_pr_commit_messages_warning(logger, owner, repo, pr_number, e)
                break
            batch = commit_messages_from_commit_list(data)
            out.extend(batch)
            if not isinstance(data, list) or len(data) < limit:
                break
            page += 1
        return out

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
        """Update the PR body (and optionally title) via PATCH /repos/.../pulls/{index}."""
        self._patch_pr_description(
            path=f"/repos/{owner}/{repo}/pulls/{pr_number}",
            description=description,
            title=title,
        )

    def capabilities(self) -> ProviderCapabilities:
        """
        Return provider capability flags.

        Gitea does not support resolving/unresolving PR review comments.
        """
        return ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=True,
            supports_review_decisions=True,
            supports_bot_blocking_state_query=True,
            supports_bot_attribution_identity_query=True,
        )
