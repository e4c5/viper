"""Fail-fast validation when context-aware review is enabled."""

from __future__ import annotations

from code_review.config import ContextAwareReviewConfig, SCMConfig
from code_review.context.errors import ContextAwareFatalError


def validate_context_aware_sources(
    ctx: ContextAwareReviewConfig,
    scm: SCMConfig,
) -> None:
    """
    When CONTEXT_AWARE_REVIEW_ENABLED is true, require DB URL and complete
    configuration for every enabled source.
    """
    if not ctx.enabled:
        return
    if not (ctx.db_url and ctx.db_url.strip()):
        raise ContextAwareFatalError(
            "CONTEXT_AWARE_REVIEW_ENABLED is true but CONTEXT_AWARE_REVIEW_DB_URL is missing."
        )
    if ctx.github_issues_enabled:
        has_gh_token = ctx.github_token and ctx.github_token.get_secret_value()
        if scm.provider != "github" and not has_gh_token:
            raise ContextAwareFatalError(
                "CONTEXT_GITHUB_ISSUES_ENABLED requires SCM_PROVIDER=github with SCM_TOKEN, or "
                "CONTEXT_GITHUB_TOKEN (and CONTEXT_GITHUB_API_URL if not using api.github.com)."
            )
    if ctx.jira_enabled:
        if not ctx.jira_url:
            raise ContextAwareFatalError("CONTEXT_JIRA_ENABLED requires CONTEXT_JIRA_URL.")
        if not ctx.jira_email.strip():
            raise ContextAwareFatalError("CONTEXT_JIRA_ENABLED requires CONTEXT_JIRA_EMAIL.")
        if not ctx.jira_token or not ctx.jira_token.get_secret_value():
            raise ContextAwareFatalError("CONTEXT_JIRA_ENABLED requires CONTEXT_JIRA_TOKEN.")
    if ctx.confluence_enabled:
        if not ctx.confluence_url:
            raise ContextAwareFatalError(
                "CONTEXT_CONFLUENCE_ENABLED requires CONTEXT_CONFLUENCE_URL."
            )
        if not ctx.confluence_email.strip():
            raise ContextAwareFatalError(
                "CONTEXT_CONFLUENCE_ENABLED requires CONTEXT_CONFLUENCE_EMAIL."
            )
        if not ctx.confluence_token or not ctx.confluence_token.get_secret_value():
            raise ContextAwareFatalError(
                "CONTEXT_CONFLUENCE_ENABLED requires CONTEXT_CONFLUENCE_TOKEN."
            )
