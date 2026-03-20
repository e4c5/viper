"""Fail-fast validation when context-aware review is enabled."""

from __future__ import annotations

from code_review.config import ContextAwareReviewConfig, SCMConfig
from code_review.context.errors import ContextAwareFatalError


def _require(value: bool, message: str) -> None:
    if not value:
        raise ContextAwareFatalError(message)


def _validate_enabled_github_source(ctx: ContextAwareReviewConfig, scm: SCMConfig) -> None:
    if not ctx.github_issues_enabled:
        return
    has_gh_token = bool(ctx.github_token and ctx.github_token.get_secret_value())
    _require(
        scm.provider == "github" or has_gh_token,
        (
            "CONTEXT_GITHUB_ISSUES_ENABLED requires SCM_PROVIDER=github with SCM_TOKEN, or "
            "CONTEXT_GITHUB_TOKEN (and CONTEXT_GITHUB_API_URL if not using api.github.com)."
        ),
    )


def _validate_enabled_jira_source(ctx: ContextAwareReviewConfig) -> None:
    if not ctx.jira_enabled:
        return
    _require(bool(ctx.jira_url), "CONTEXT_JIRA_ENABLED requires CONTEXT_JIRA_URL.")
    _require(bool(ctx.jira_email.strip()), "CONTEXT_JIRA_ENABLED requires CONTEXT_JIRA_EMAIL.")
    _require(
        bool(ctx.jira_token and ctx.jira_token.get_secret_value()),
        "CONTEXT_JIRA_ENABLED requires CONTEXT_JIRA_TOKEN.",
    )


def _validate_enabled_confluence_source(ctx: ContextAwareReviewConfig) -> None:
    if not ctx.confluence_enabled:
        return
    _require(
        bool(ctx.confluence_url),
        "CONTEXT_CONFLUENCE_ENABLED requires CONTEXT_CONFLUENCE_URL.",
    )
    _require(
        bool(ctx.confluence_email.strip()),
        "CONTEXT_CONFLUENCE_ENABLED requires CONTEXT_CONFLUENCE_EMAIL.",
    )
    _require(
        bool(ctx.confluence_token and ctx.confluence_token.get_secret_value()),
        "CONTEXT_CONFLUENCE_ENABLED requires CONTEXT_CONFLUENCE_TOKEN.",
    )


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
    _require(
        bool(ctx.db_url and ctx.db_url.strip()),
        "CONTEXT_AWARE_REVIEW_ENABLED is true but CONTEXT_AWARE_REVIEW_DB_URL is missing.",
    )
    _validate_enabled_github_source(ctx, scm)
    _validate_enabled_jira_source(ctx)
    _validate_enabled_confluence_source(ctx)
