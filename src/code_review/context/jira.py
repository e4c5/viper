"""Fetch Jira ticket content (summary, description, comments) for context enrichment."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_MAX_COMMENT_CHARS = 1_000
_MAX_COMMENTS = 10


class JiraFetcher:
    """Fetch Jira issue details using the Jira REST API v3 (Cloud) or v2 (Server/DC).

    Authentication: HTTP Basic Auth with email and API token.
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        token: str,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = (email, token)
        self._timeout = timeout

    def fetch_ticket(self, ticket_key: str) -> str:
        """Return a human-readable summary of the Jira ticket.

        Returns an empty string on any error so the review can proceed without context.
        """
        try:
            return self._fetch_ticket(ticket_key)
        except Exception as exc:
            logger.debug("JiraFetcher: failed to fetch %s: %s", ticket_key, exc)
            return ""

    def _fetch_ticket(self, ticket_key: str) -> str:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(
                f"{self._base_url}/rest/api/2/issue/{ticket_key}",
                auth=self._auth,
                params={
                    "fields": "summary,description,status,issuetype,priority,labels,comment"
                },
            )
            r.raise_for_status()
            data = r.json()

        fields = data.get("fields") or {}
        summary = fields.get("summary", "")
        status = (fields.get("status") or {}).get("name", "")
        issue_type = (fields.get("issuetype") or {}).get("name", "")
        priority = (fields.get("priority") or {}).get("name", "")
        labels = fields.get("labels") or []

        description = self._render_description(fields.get("description"))

        lines = [f"Jira {ticket_key} [{issue_type}] ({status}): {summary}"]
        if priority:
            lines.append(f"Priority: {priority}")
        if labels:
            lines.append(f"Labels: {', '.join(labels)}")
        if description:
            lines.append(f"\n{description}")

        # Comments
        comment_data = (fields.get("comment") or {}).get("comments", [])
        comments = self._render_comments(comment_data)
        if comments:
            lines.append("\n--- Comments ---")
            lines.extend(comments)

        return "\n".join(lines)

    @staticmethod
    def _render_description(desc: object) -> str:
        """Convert Jira description (string or Atlassian Document Format dict) to plain text."""
        if desc is None:
            return ""
        if isinstance(desc, str):
            return desc.strip()
        # Atlassian Document Format (ADF): traverse content nodes
        if isinstance(desc, dict):
            return _adf_to_text(desc).strip()
        return str(desc)

    @staticmethod
    def _render_comments(comments: list) -> list[str]:
        result: list[str] = []
        for c in comments[:_MAX_COMMENTS]:
            author = (c.get("author") or {}).get("displayName", "unknown")
            body = JiraFetcher._render_description(c.get("body"))
            if body:
                truncated = body[:_MAX_COMMENT_CHARS]
                if len(body) > _MAX_COMMENT_CHARS:
                    truncated += " [truncated]"
                result.append(f"{author}: {truncated}")
        return result


def _adf_to_text(node: dict) -> str:
    """Recursively extract plain text from an Atlassian Document Format node."""
    node_type = node.get("type", "")
    text = node.get("text", "")
    content = node.get("content", [])

    parts: list[str] = []
    if text:
        parts.append(text)
    for child in content:
        if isinstance(child, dict):
            parts.append(_adf_to_text(child))

    separator = "\n" if node_type in ("paragraph", "heading", "bulletList", "orderedList") else ""
    return separator.join(parts)
