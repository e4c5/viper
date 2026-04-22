"""Unit tests for reference extraction and context config validation."""

import os

import pytest

from code_review.config import SCMConfig, get_context_aware_config, reset_config_cache
from code_review.context.errors import ContextAwareFatalError
from code_review.context.extract import extract_confluence_refs, extract_context_references
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


def test_extract_does_not_treat_gitlab_issue_url_as_github():
    refs = extract_context_references(
        "github",
        "myorg",
        "myrepo",
        ["See https://gitlab.com/other/r/issues/99"],
        extract_jira=False,
        extract_confluence=False,
    )
    assert not any(r.ref_type == ReferenceType.GITHUB_ISSUE for r in refs)


def test_extract_gitlab_issue_url():
    refs = extract_context_references(
        "gitlab",
        "myorg",
        "myrepo",
        ["See https://gitlab.com/group/sub/repo/-/issues/55"],
        extract_github=False,
        extract_jira=False,
        extract_confluence=False,
    )
    assert len(refs) == 1
    assert refs[0].ref_type == ReferenceType.GITLAB_ISSUE
    assert refs[0].external_id == "group/sub/repo#55"


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


def test_extract_confluence_page_url():
    refs = extract_context_references(
        "github",
        "o",
        "r",
        ["See https://example.atlassian.net/wiki/spaces/ENG/pages/12345/Page+Title for details"],
        extract_github=False,
        extract_jira=False,
    )
    assert any(r.ref_type == ReferenceType.CONFLUENCE and r.external_id == "12345" for r in refs)


def test_extract_confluence_viewpage_action_url():
    refs = extract_context_references(
        "github",
        "o",
        "r",
        ["See https://wiki.example.com/pages/viewpage.action?pageId=99887 for context"],
        extract_github=False,
        extract_jira=False,
    )
    assert any(r.ref_type == ReferenceType.CONFLUENCE and r.external_id == "99887" for r in refs)


def test_extract_confluence_deduplicates_same_page_id():
    refs = extract_context_references(
        "github",
        "o",
        "r",
        [
            "https://wiki.example.com/spaces/ENG/pages/42/Spec",
            "https://wiki.example.com/pages/viewpage.action?pageId=42",
        ],
        extract_github=False,
        extract_jira=False,
    )
    confluence_refs = [r for r in refs if r.ref_type == ReferenceType.CONFLUENCE]
    assert len(confluence_refs) == 1
    assert confluence_refs[0].external_id == "42"


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


def test_extract_ignores_non_string_segments():
    refs = extract_context_references(
        "github",
        "org",
        "repo",
        [object(), None, "PROJ-123"],
        extract_github=False,
        extract_confluence=False,
    )

    assert [r.external_id for r in refs] == ["PROJ-123"]


# ---------------------------------------------------------------------------
# extract_confluence_refs (standalone, for transitive following)
# ---------------------------------------------------------------------------


def test_extract_confluence_refs_from_text():
    text = "See https://wiki.example.com/spaces/ENG/pages/555/Design for details"
    refs = extract_confluence_refs(text)
    assert len(refs) == 1
    assert refs[0].ref_type == ReferenceType.CONFLUENCE
    assert refs[0].external_id == "555"


def test_extract_confluence_refs_excludes_known_ids():
    text = (
        "https://wiki.example.com/spaces/ENG/pages/111/A "
        "https://wiki.example.com/spaces/ENG/pages/222/B"
    )
    refs = extract_confluence_refs(text, exclude_ids={"111"})
    assert len(refs) == 1
    assert refs[0].external_id == "222"


def test_extract_confluence_refs_deduplicates():
    text = (
        "https://wiki.example.com/spaces/ENG/pages/100/A "
        "https://wiki.example.com/pages/viewpage.action?pageId=100"
    )
    refs = extract_confluence_refs(text)
    assert len(refs) == 1


def test_extract_confluence_refs_returns_empty_for_no_links():
    refs = extract_confluence_refs("No links here, just PROJ-123")
    assert refs == []


def test_extract_confluence_refs_skips_fenced_code_links():
    text = "Example only:\n```\nhttps://wiki.example.com/spaces/ENG/pages/999/StackTrace\n```\n"

    assert extract_confluence_refs(text) == []


def test_extract_confluence_refs_keeps_links_outside_fenced_code():
    text = (
        "Ignore example:\n"
        "```\n"
        "https://wiki.example.com/spaces/ENG/pages/999/StackTrace\n"
        "```\n"
        "Use https://wiki.example.com/spaces/ENG/pages/1000/Spec instead."
    )

    refs = extract_confluence_refs(text)

    assert [ref.external_id for ref in refs] == ["1000"]


def _clear_context_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("CONTEXT_") or key.startswith("CONTEXT_AWARE"):
            monkeypatch.delenv(key, raising=False)


def test_validate_allows_missing_db_when_enabled(monkeypatch):
    _clear_context_env(monkeypatch)
    monkeypatch.setenv("CONTEXT_AWARE_REVIEW_ENABLED", "true")
    monkeypatch.delenv("CONTEXT_AWARE_REVIEW_DB_URL", raising=False)
    reset_config_cache()
    ctx = get_context_aware_config()
    validate_context_aware_sources(ctx, SCMConfig(url="https://gitea/x", token="t"))


def test_validate_github_issues_non_github_without_token(monkeypatch):
    _clear_context_env(monkeypatch)
    monkeypatch.setenv("CONTEXT_AWARE_REVIEW_ENABLED", "true")
    monkeypatch.setenv("CONTEXT_AWARE_REVIEW_DB_URL", "postgresql://u:a@h/db")
    monkeypatch.setenv("CONTEXT_GITHUB_ISSUES_ENABLED", "true")
    monkeypatch.delenv("CONTEXT_GITHUB_TOKEN", raising=False)
    reset_config_cache()
    ctx = get_context_aware_config()
    scm = SCMConfig(url="https://gitea/x", token="t")
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_GITHUB_TOKEN"):
        validate_context_aware_sources(ctx, scm)


def test_validate_gitlab_issues_non_gitlab_without_token(monkeypatch):
    _clear_context_env(monkeypatch)
    monkeypatch.setenv("CONTEXT_AWARE_REVIEW_ENABLED", "true")
    monkeypatch.setenv("CONTEXT_AWARE_REVIEW_DB_URL", "postgresql://u:a@h/db")
    monkeypatch.setenv("CONTEXT_GITHUB_ISSUES_ENABLED", "false")
    monkeypatch.setenv("CONTEXT_GITLAB_ISSUES_ENABLED", "true")
    monkeypatch.delenv("CONTEXT_GITLAB_TOKEN", raising=False)
    reset_config_cache()
    ctx = get_context_aware_config()
    scm = SCMConfig(url="https://gitea/x", token="t")
    with pytest.raises(ContextAwareFatalError, match="CONTEXT_GITLAB_TOKEN"):
        validate_context_aware_sources(ctx, scm)


@pytest.fixture(autouse=True)
def _reset_cfg():
    reset_config_cache()
    yield
    reset_config_cache()
