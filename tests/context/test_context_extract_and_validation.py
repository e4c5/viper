"""Unit tests for reference extraction and context config validation."""

import os

import pytest

from code_review.config import SCMConfig, get_context_aware_config, reset_config_cache
from code_review.context.errors import ContextAwareFatalError
from code_review.context.extract import extract_context_references
from code_review.context.types import ReferenceType
from code_review.context.validation import validate_context_aware_sources


def test_extract_github_issue_url_only():
    refs = extract_context_references(
        "github",
        "myorg",
        "myrepo",
        ["See https://github.com/other/r/issues/99"],
        extract_jira=False,
        extract_confluence=False,
    )
    assert len(refs) == 1
    assert refs[0].ref_type == ReferenceType.GITHUB_ISSUE
    assert refs[0].external_id == "other/r#99"


def test_extract_hash_issue_github_same_repo():
    refs = extract_context_references(
        "github",
        "o",
        "r",
        ["Fixes #12 and cleanup"],
        extract_jira=False,
        extract_confluence=False,
    )
    assert any(r.external_id == "o/r#12" for r in refs)


def test_extract_not_hash_on_gitea():
    refs = extract_context_references(
        "gitea",
        "o",
        "r",
        ["Maybe #12 is a markdown heading"],
        extract_jira=False,
        extract_confluence=False,
    )
    assert not any(r.ref_type == ReferenceType.GITHUB_ISSUE for r in refs)


def test_extract_jira_key():
    refs = extract_context_references(
        "github",
        "o",
        "r",
        ["Fixes PROJ-123"],
        extract_github=False,
        extract_confluence=False,
    )
    assert len(refs) == 1
    assert refs[0].ref_type == ReferenceType.JIRA


def test_extract_skips_fenced_code():
    refs = extract_context_references(
        "github",
        "o",
        "r",
        ["```\nPROJ-999\n```\nReal FIX-1 in prose"],
        extract_github=False,
        extract_confluence=False,
    )
    keys = {r.external_id for r in refs}
    assert "FIX-1" in keys
    assert "PROJ-999" not in keys


def _clear_context_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ.keys()):
        if key.startswith("CONTEXT_") or key.startswith("CONTEXT_AWARE"):
            monkeypatch.delenv(key, raising=False)


def test_validate_requires_db_when_enabled(monkeypatch):
    _clear_context_env(monkeypatch)
    monkeypatch.setenv("CONTEXT_AWARE_REVIEW_ENABLED", "true")
    monkeypatch.delenv("CONTEXT_AWARE_REVIEW_DB_URL", raising=False)
    reset_config_cache()
    ctx = get_context_aware_config()
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_AWARE_REVIEW_DB_URL"):
        validate_context_aware_sources(ctx, SCMConfig(url="https://gitea/x", token="t"))


def test_validate_github_issues_non_github_without_token(monkeypatch):
    _clear_context_env(monkeypatch)
    monkeypatch.setenv("CONTEXT_AWARE_REVIEW_ENABLED", "true")
    monkeypatch.setenv("CONTEXT_AWARE_REVIEW_DB_URL", "postgresql://u@h/db")
    monkeypatch.setenv("CONTEXT_GITHUB_ISSUES_ENABLED", "true")
    monkeypatch.delenv("CONTEXT_GITHUB_TOKEN", raising=False)
    reset_config_cache()
    ctx = get_context_aware_config()
    scm = SCMConfig(url="https://gitea/x", token="t")
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_GITHUB_TOKEN"):
        validate_context_aware_sources(ctx, scm)


@pytest.fixture(autouse=True)
def _reset_cfg():
    reset_config_cache()
    yield
    reset_config_cache()
