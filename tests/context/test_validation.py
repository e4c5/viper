"""Unit tests for context/validation.py."""

from unittest.mock import MagicMock

import pytest

from code_review.context.errors import ContextAwareFatalError
from code_review.context.validation import validate_context_aware_sources


def _secret(value: str):
    m = MagicMock()
    m.get_secret_value.return_value = value
    return m


def _make_ctx(**overrides):
    ctx = MagicMock()
    ctx.enabled = True
    ctx.db_url = "postgresql://u:p@host/db"
    ctx.github_issues_enabled = False
    ctx.gitlab_issues_enabled = False
    ctx.jira_enabled = False
    ctx.confluence_enabled = False
    ctx.github_token = None
    ctx.github_api_url = None
    ctx.gitlab_token = None
    ctx.gitlab_api_url = None
    ctx.jira_url = ""
    ctx.jira_email = ""
    ctx.jira_token = None
    ctx.confluence_url = ""
    ctx.confluence_email = ""
    ctx.confluence_token = None
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


def _make_scm(provider="github"):
    scm = MagicMock()
    scm.provider = provider
    scm.token = _secret("tok")
    return scm


# ---------------------------------------------------------------------------
# disabled / short-circuit
# ---------------------------------------------------------------------------


def test_disabled_returns_immediately():
    ctx = _make_ctx(enabled=False, db_url="")  # missing db_url but disabled
    validate_context_aware_sources(ctx, _make_scm())  # must not raise


# ---------------------------------------------------------------------------
# optional db_url
# ---------------------------------------------------------------------------


def test_missing_db_url_passes_direct_distillation_mode():
    ctx = _make_ctx(db_url="")
    validate_context_aware_sources(ctx, _make_scm())  # must not raise


def test_whitespace_db_url_passes_direct_distillation_mode():
    ctx = _make_ctx(db_url="   ")
    validate_context_aware_sources(ctx, _make_scm())  # must not raise


# ---------------------------------------------------------------------------
# GitHub source validation
# ---------------------------------------------------------------------------


def test_github_enabled_with_scm_github_passes():
    ctx = _make_ctx(github_issues_enabled=True)
    validate_context_aware_sources(ctx, _make_scm(provider="github"))  # no raise


def test_github_enabled_with_token_passes():
    ctx = _make_ctx(github_issues_enabled=True, github_token=_secret("ghp_xxx"))
    validate_context_aware_sources(ctx, _make_scm(provider="gitlab"))  # no raise


def test_github_enabled_no_token_no_github_scm_raises():
    ctx = _make_ctx(github_issues_enabled=True, github_token=None)
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_GITHUB_ISSUES_ENABLED"):
        validate_context_aware_sources(ctx, _make_scm(provider="gitlab"))


# ---------------------------------------------------------------------------
# GitLab source validation
# ---------------------------------------------------------------------------


def test_gitlab_enabled_with_scm_gitlab_passes():
    ctx = _make_ctx(gitlab_issues_enabled=True)
    validate_context_aware_sources(ctx, _make_scm(provider="gitlab"))  # no raise


def test_gitlab_enabled_with_token_passes():
    ctx = _make_ctx(gitlab_issues_enabled=True, gitlab_token=_secret("glpat-xxx"))
    validate_context_aware_sources(ctx, _make_scm(provider="github"))  # no raise


def test_gitlab_enabled_no_token_no_gitlab_scm_raises():
    ctx = _make_ctx(gitlab_issues_enabled=True, gitlab_token=None)
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_GITLAB_ISSUES_ENABLED"):
        validate_context_aware_sources(ctx, _make_scm(provider="github"))


# ---------------------------------------------------------------------------
# Jira source validation
# ---------------------------------------------------------------------------


def test_jira_enabled_with_all_creds_passes():
    ctx = _make_ctx(
        jira_enabled=True,
        jira_url="https://jira.example.com",
        jira_email="user@example.com",
        jira_token=_secret("jira-tok"),
    )
    validate_context_aware_sources(ctx, _make_scm())  # no raise


def test_jira_enabled_missing_url_raises():
    ctx = _make_ctx(jira_enabled=True, jira_url="", jira_email="u@e.com", jira_token=_secret("t"))
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_JIRA_URL"):
        validate_context_aware_sources(ctx, _make_scm())


def test_jira_enabled_missing_email_raises():
    ctx = _make_ctx(
        jira_enabled=True,
        jira_url="https://jira.example.com",
        jira_email="",
        jira_token=_secret("t"),
    )
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_JIRA_EMAIL"):
        validate_context_aware_sources(ctx, _make_scm())


def test_jira_enabled_missing_token_raises():
    ctx = _make_ctx(
        jira_enabled=True,
        jira_url="https://jira.example.com",
        jira_email="user@example.com",
        jira_token=None,
    )
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_JIRA_TOKEN"):
        validate_context_aware_sources(ctx, _make_scm())


# ---------------------------------------------------------------------------
# Confluence source validation
# ---------------------------------------------------------------------------


def test_confluence_enabled_with_all_creds_passes():
    ctx = _make_ctx(
        confluence_enabled=True,
        confluence_url="https://wiki.example.com",
        confluence_email="user@example.com",
        confluence_token=_secret("conf-tok"),
    )
    validate_context_aware_sources(ctx, _make_scm())  # no raise


def test_confluence_enabled_missing_url_raises():
    ctx = _make_ctx(
        confluence_enabled=True,
        confluence_url="",
        confluence_email="u@e.com",
        confluence_token=_secret("t"),
    )
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_CONFLUENCE_URL"):
        validate_context_aware_sources(ctx, _make_scm())


def test_confluence_enabled_missing_email_raises():
    ctx = _make_ctx(
        confluence_enabled=True,
        confluence_url="https://wiki.example.com",
        confluence_email="",
        confluence_token=_secret("t"),
    )
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_CONFLUENCE_EMAIL"):
        validate_context_aware_sources(ctx, _make_scm())


def test_confluence_enabled_missing_token_raises():
    ctx = _make_ctx(
        confluence_enabled=True,
        confluence_url="https://wiki.example.com",
        confluence_email="user@example.com",
        confluence_token=None,
    )
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_CONFLUENCE_TOKEN"):
        validate_context_aware_sources(ctx, _make_scm())
