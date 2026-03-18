"""Orchestrate context fetching from GitHub Issues, Jira, and Confluence.

The ContextFetcher:
1. Collects commit messages and the PR description.
2. Extracts references (GitHub issue numbers, Jira ticket keys, Confluence URLs).
3. Fetches each reference, respecting a configurable token budget.
4. Returns a formatted context string to inject into the review prompt.

When the aggregated context would exceed the token budget the content is
truncated by item priority (GitHub Issues first, then Jira, then Confluence)
so the most relevant context is always included.
"""

from __future__ import annotations

import logging

from code_review.context.extractor import Reference, ReferenceType, extract_references

logger = logging.getLogger(__name__)

# Characters-per-token approximation (matches _estimate_tokens in runner.py)
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return max(0, len(text) // _CHARS_PER_TOKEN)


class ContextFetcher:
    """Fetch and assemble context from linked issues/tickets/pages.

    Parameters
    ----------
    scm_base_url:
        Base URL of the SCM API (used to build GitHub Issues API calls).
    scm_token:
        Token for authenticating against the SCM API.
    context_config:
        A :class:`~code_review.config.ContextConfig` instance.
    owner / repo:
        Current repository coordinates (used to resolve short-form issue refs).
    """

    def __init__(
        self,
        scm_base_url: str,
        scm_token: str,
        context_config: object,  # ContextConfig — avoid circular import
        owner: str = "",
        repo: str = "",
    ) -> None:
        self._scm_base_url = scm_base_url
        self._scm_token = scm_token
        self._cfg = context_config
        self._owner = owner
        self._repo = repo

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_context(
        self,
        pr_description: str,
        pr_title: str,
        commit_messages: list[str],
    ) -> str:
        """Return a formatted context string (may be empty).

        The string is ready for injection into the review prompt between
        ``<context>`` tags.
        """
        if not getattr(self._cfg, "enabled", False):
            return ""

        # Combine all text sources for reference extraction
        all_text = "\n".join(filter(None, [pr_title, pr_description, *commit_messages]))

        jira_keys: list[str] | None = None
        raw_jira_keys = getattr(self._cfg, "jira_project_keys", None)
        if raw_jira_keys:
            jira_keys = [k.strip().upper() for k in raw_jira_keys.split(",") if k.strip()]

        refs = extract_references(
            all_text,
            owner=self._owner,
            repo=self._repo,
            jira_project_keys=jira_keys,
        )

        if not refs:
            return ""

        items: list[str] = []
        budget = getattr(self._cfg, "max_context_tokens", 20_000)
        used = 0

        for ref in refs:
            if used >= budget:
                logger.debug("Context budget exhausted after %d tokens", used)
                break
            content = self._fetch_reference(ref)
            if not content:
                continue
            # Truncate this item if it would exceed the remaining budget
            remaining_chars = (budget - used) * _CHARS_PER_TOKEN
            if len(content) > remaining_chars:
                content = content[:remaining_chars] + "\n[truncated — context budget exceeded]"

            items.append(content)
            used += _estimate_tokens(content)

        if not items:
            return ""

        joined = "\n\n---\n\n".join(items)
        return f"<context>\n{joined}\n</context>"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_reference(self, ref: Reference) -> str:
        """Dispatch to the appropriate fetcher for the reference type."""
        if ref.ref_type == ReferenceType.GITHUB_ISSUE:
            return self._fetch_github_issue(ref)
        if ref.ref_type == ReferenceType.JIRA:
            return self._fetch_jira_ticket(ref)
        if ref.ref_type == ReferenceType.CONFLUENCE:
            return self._fetch_confluence_page(ref)
        return ""

    def _fetch_github_issue(self, ref: Reference) -> str:
        if not getattr(self._cfg, "github_issues_enabled", True):
            return ""
        try:
            from code_review.context.github_issues import GitHubIssuesFetcher

            parts = ref.identifier.split("/")
            if len(parts) != 3:
                return ""
            gh_owner, gh_repo, number_str = parts
            fetcher = GitHubIssuesFetcher(
                base_url=self._scm_base_url,
                token=self._scm_token,
            )
            return fetcher.fetch_issue(gh_owner, gh_repo, int(number_str))
        except Exception as exc:
            logger.debug("Failed to fetch GitHub issue %s: %s", ref.identifier, exc)
            return ""

    def _fetch_jira_ticket(self, ref: Reference) -> str:
        if not getattr(self._cfg, "jira_enabled", False):
            return ""
        jira_url = getattr(self._cfg, "jira_url", None)
        jira_email = getattr(self._cfg, "jira_email", None)
        jira_token = getattr(self._cfg, "jira_token", None)
        if not (jira_url and jira_email and jira_token):
            logger.debug("Jira context enabled but credentials not fully configured")
            return ""
        try:
            from code_review.context.jira import JiraFetcher

            token_val = (
                jira_token.get_secret_value()
                if hasattr(jira_token, "get_secret_value")
                else str(jira_token)
            )
            fetcher = JiraFetcher(
                base_url=jira_url,
                email=jira_email,
                token=token_val,
            )
            return fetcher.fetch_ticket(ref.identifier)
        except Exception as exc:
            logger.debug("Failed to fetch Jira ticket %s: %s", ref.identifier, exc)
            return ""

    def _fetch_confluence_page(self, ref: Reference) -> str:
        if not getattr(self._cfg, "confluence_enabled", False):
            return ""
        confluence_url = getattr(self._cfg, "confluence_url", None)
        confluence_email = getattr(self._cfg, "confluence_email", None)
        confluence_token = getattr(self._cfg, "confluence_token", None)
        if not (confluence_url and confluence_email and confluence_token):
            logger.debug("Confluence context enabled but credentials not fully configured")
            return ""
        try:
            from code_review.context.confluence import ConfluenceFetcher

            token_val = (
                confluence_token.get_secret_value()
                if hasattr(confluence_token, "get_secret_value")
                else str(confluence_token)
            )
            fetcher = ConfluenceFetcher(
                base_url=confluence_url,
                email=confluence_email,
                token=token_val,
            )
            # identifier may be a page ID or a full URL
            identifier = ref.identifier
            if identifier.isdigit():
                return fetcher.fetch_page_by_id(identifier)
            return fetcher.fetch_page_by_url(ref.url or identifier)
        except Exception as exc:
            logger.debug("Failed to fetch Confluence page %s: %s", ref.identifier, exc)
            return ""
