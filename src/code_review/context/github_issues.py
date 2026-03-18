"""Fetch GitHub Issue content (title, body, labels, comments) for context enrichment."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_MAX_COMMENT_CHARS = 1_000  # truncate individual comments to keep context concise
_MAX_COMMENTS = 10  # max number of comments to include


class GitHubIssuesFetcher:
    """Fetch GitHub issue details using the REST API.

    Uses the same token as the SCM provider so no extra credentials are needed
    when the PR lives in the same GitHub instance.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def fetch_issue(self, owner: str, repo: str, issue_number: int) -> str:
        """Return a human-readable summary of the issue for inclusion in the review prompt.

        Returns an empty string on any error so the review can proceed without context.
        """
        try:
            return self._fetch_issue(owner, repo, issue_number)
        except Exception as exc:
            logger.debug(
                "GitHubIssuesFetcher: failed to fetch %s/%s#%d: %s",
                owner,
                repo,
                issue_number,
                exc,
            )
            return ""

    def _fetch_issue(self, owner: str, repo: str, issue_number: int) -> str:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(
                f"{self._base_url}/repos/{owner}/{repo}/issues/{issue_number}",
                headers=self._headers(),
            )
            r.raise_for_status()
            data = r.json()

        title = data.get("title", "")
        body = (data.get("body") or "").strip()
        state = data.get("state", "")
        labels = [
            lb.get("name", lb) if isinstance(lb, dict) else str(lb)
            for lb in (data.get("labels") or [])
        ]

        lines = [
            f"GitHub Issue #{issue_number} ({state}): {title}",
        ]
        if labels:
            lines.append(f"Labels: {', '.join(labels)}")
        if body:
            lines.append(f"\n{body}")

        # Fetch top comments
        comments = self._fetch_comments(client, owner, repo, issue_number)
        if comments:
            lines.append("\n--- Comments ---")
            lines.extend(comments)

        return "\n".join(lines)

    def _fetch_comments(
        self,
        client: httpx.Client,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> list[str]:
        try:
            r = client.get(
                f"{self._base_url}/repos/{owner}/{repo}/issues/{issue_number}/comments",
                headers=self._headers(),
                params={"per_page": _MAX_COMMENTS},
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            return []

        result: list[str] = []
        for comment in data[:_MAX_COMMENTS]:
            author = comment.get("user", {}).get("login", "unknown")
            body = (comment.get("body") or "").strip()
            if body:
                truncated = body[:_MAX_COMMENT_CHARS]
                if len(body) > _MAX_COMMENT_CHARS:
                    truncated += " [truncated]"
                result.append(f"@{author}: {truncated}")
        return result
