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
    _require(bool(ctx.atlassian_url), "CONTEXT_JIRA_ENABLED requires CONTEXT_ATLASSIAN_URL.")
    _require(
        bool(ctx.atlassian_email.strip()),
        "CONTEXT_JIRA_ENABLED requires CONTEXT_ATLASSIAN_EMAIL.",
    )
    _require(
        bool(ctx.atlassian_token and ctx.atlassian_token.get_secret_value()),
        "CONTEXT_JIRA_ENABLED requires CONTEXT_ATLASSIAN_TOKEN.",
    )


def _validate_enabled_gitlab_source(ctx: ContextAwareReviewConfig, scm: SCMConfig) -> None:
    if not ctx.gitlab_issues_enabled:
        return
    has_gl_token = bool(ctx.gitlab_token and ctx.gitlab_token.get_secret_value())
    _require(
        scm.provider == "gitlab" or has_gl_token,
        (
            "CONTEXT_GITLAB_ISSUES_ENABLED requires SCM_PROVIDER=gitlab with SCM_TOKEN, or "
            "CONTEXT_GITLAB_TOKEN (and CONTEXT_GITLAB_API_URL if not using SCM_URL)."
        ),
    )


def _validate_enabled_confluence_source(ctx: ContextAwareReviewConfig) -> None:
    if not ctx.confluence_enabled:
        return
    _require(
        bool(ctx.atlassian_url),
        "CONTEXT_CONFLUENCE_ENABLED requires CONTEXT_ATLASSIAN_URL.",
    )
    _require(
        bool(ctx.atlassian_email.strip()),
        "CONTEXT_CONFLUENCE_ENABLED requires CONTEXT_ATLASSIAN_EMAIL.",
    )
    _require(
        bool(ctx.atlassian_token and ctx.atlassian_token.get_secret_value()),
        "CONTEXT_CONFLUENCE_ENABLED requires CONTEXT_ATLASSIAN_TOKEN.",
    )


def validate_context_aware_sources(
    ctx: ContextAwareReviewConfig,
    scm: SCMConfig,
) -> None:
    """
    When CONTEXT_AWARE_REVIEW_ENABLED is true, require complete configuration
    for every enabled source.

    CONTEXT_AWARE_REVIEW_DB_URL is optional. When omitted, context documents are
    fetched directly and distilled without cache/RAG storage.
    """
    if not ctx.enabled:
        return
    _validate_enabled_github_source(ctx, scm)
    _validate_enabled_gitlab_source(ctx, scm)
    _validate_enabled_jira_source(ctx)
    _validate_enabled_confluence_source(ctx)
