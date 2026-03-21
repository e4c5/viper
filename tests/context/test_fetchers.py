"""Unit tests for context fetchers (mocked HTTP via httpx)."""

from unittest.mock import MagicMock, patch

import pytest

from code_review.context.errors import ContextAwareAuthError, ContextAwareFatalError
from code_review.context.fetchers import (
    _adf_to_plain,
    _strip_html_to_text,
    fetch_confluence_page,
    fetch_github_issue,
    fetch_gitlab_issue,
    fetch_jira_issue,
    fetch_reference,
)
from code_review.context.types import ContextReference, ReferenceType


def _mock_httpx_response(status_code: int, json_data=None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


def _patch_client(response):
    """Patch httpx.Client so .get() returns *response* regardless of args."""
    client_mock = MagicMock()
    client_mock.__enter__ = MagicMock(return_value=client_mock)
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.return_value = response
    return patch("httpx.Client", return_value=client_mock)


# ---------------------------------------------------------------------------
# fetch_github_issue
# ---------------------------------------------------------------------------


def test_fetch_github_issue_happy_path():
    data = {
        "title": "Bug: crash on login",
        "body": "Steps to reproduce…",
        "state": "open",
        "labels": [{"name": "bug"}, {"name": "urgent"}],
        "html_url": "https://github.com/org/repo/issues/42",
        "id": 1234,
        "updated_at": "2024-01-01T00:00:00Z",
    }
    with _patch_client(_mock_httpx_response(200, data)):
        doc = fetch_github_issue("https://api.github.com", "tok", "org", "repo", "42")
    assert doc is not None
    assert doc.title == "Bug: crash on login"
    assert "bug" in doc.body
    assert doc.external_id == "org/repo#42"
    assert doc.external_updated_at == "2024-01-01T00:00:00Z"


def test_fetch_github_issue_404_returns_none():
    with _patch_client(_mock_httpx_response(404)):
        doc = fetch_github_issue("https://api.github.com", "tok", "org", "repo", "99")
    assert doc is None


def test_fetch_github_issue_401_raises_auth_error():
    with _patch_client(_mock_httpx_response(401, text="Unauthorized")):
        with pytest.raises(ContextAwareAuthError):
            fetch_github_issue("https://api.github.com", "bad-tok", "org", "repo", "1")


def test_fetch_github_issue_403_raises_auth_error():
    with _patch_client(_mock_httpx_response(403, text="Forbidden")):
        with pytest.raises(ContextAwareAuthError):
            fetch_github_issue("https://api.github.com", "tok", "org", "repo", "1")


def test_fetch_github_issue_500_raises_fatal():
    with _patch_client(_mock_httpx_response(500, text="Internal Server Error")):
        with pytest.raises(ContextAwareFatalError):
            fetch_github_issue("https://api.github.com", "tok", "org", "repo", "1")


# ---------------------------------------------------------------------------
# fetch_gitlab_issue
# ---------------------------------------------------------------------------


def test_fetch_gitlab_issue_happy_path():
    data = {
        "title": "Fix pipeline",
        "description": "The CI pipeline fails on main.",
        "state": "opened",
        "labels": ["ci", "blocker"],
        "web_url": "https://gitlab.com/group/repo/-/issues/55",
        "id": 9999,
        "updated_at": "2024-06-01T12:00:00Z",
    }
    with _patch_client(_mock_httpx_response(200, data)):
        doc = fetch_gitlab_issue("https://gitlab.com/api/v4", "glpat-tok", "group/repo", "55")
    assert doc is not None
    assert doc.title == "Fix pipeline"
    assert doc.external_id == "group/repo#55"
    assert "ci" in doc.body


def test_fetch_gitlab_issue_404_returns_none():
    with _patch_client(_mock_httpx_response(404)):
        doc = fetch_gitlab_issue("https://gitlab.com/api/v4", "tok", "group/repo", "999")
    assert doc is None


def test_fetch_gitlab_issue_401_raises_auth_error():
    with _patch_client(_mock_httpx_response(401, text="Unauthorized")):
        with pytest.raises(ContextAwareAuthError):
            fetch_gitlab_issue("https://gitlab.com/api/v4", "bad", "group/repo", "1")


# ---------------------------------------------------------------------------
# fetch_jira_issue
# ---------------------------------------------------------------------------


def _jira_adf_description(text: str) -> dict:
    """Minimal ADF structure wrapping a single paragraph."""
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def test_fetch_jira_issue_happy_path():
    data = {
        "id": "10001",
        "fields": {
            "summary": "Add dark mode",
            "description": _jira_adf_description("Users want dark mode support."),
            "issuetype": {"name": "Story"},
            "status": {"name": "In Progress"},
            "updated": "2024-03-15T09:00:00.000+0000",
        },
    }
    with _patch_client(_mock_httpx_response(200, data)):
        doc = fetch_jira_issue("https://jira.example.com", "user@example.com", "token", "PROJ-42")
    assert doc is not None
    assert doc.title == "Add dark mode"
    assert doc.external_id == "PROJ-42"
    assert "dark mode" in doc.body
    assert "Story" in doc.body


def test_fetch_jira_issue_plain_text_description():
    data = {
        "id": "10002",
        "fields": {
            "summary": "Fix login bug",
            "description": "Plain text description here.",
            "issuetype": {"name": "Bug"},
            "status": {"name": "Open"},
            "updated": None,
        },
    }
    with _patch_client(_mock_httpx_response(200, data)):
        doc = fetch_jira_issue("https://jira.example.com", "user@example.com", "token", "BUG-1")
    assert doc is not None
    assert "Plain text description" in doc.body


def test_fetch_jira_issue_404_returns_none():
    with _patch_client(_mock_httpx_response(404)):
        doc = fetch_jira_issue("https://jira.example.com", "u", "t", "PROJ-999")
    assert doc is None


def test_fetch_jira_issue_401_raises_auth_error():
    with _patch_client(_mock_httpx_response(401, text="Unauthorized")):
        with pytest.raises(ContextAwareAuthError):
            fetch_jira_issue("https://jira.example.com", "u", "bad-token", "PROJ-1")


# ---------------------------------------------------------------------------
# fetch_confluence_page
# ---------------------------------------------------------------------------


def _confluence_response(title: str, html_body: str, version_num: int = 3) -> dict:
    return {
        "title": title,
        "type": "page",
        "status": "current",
        "body": {"storage": {"value": html_body}},
        "version": {"number": version_num},
        "history": {"lastUpdated": {"when": "2024-05-20T10:00:00.000Z"}},
    }


def test_fetch_confluence_page_happy_path():
    resp_data = _confluence_response(
        "Architecture Overview",
        "<h1>Overview</h1><p>This page describes the architecture.</p>",
    )
    with _patch_client(_mock_httpx_response(200, resp_data)):
        doc = fetch_confluence_page(
            "https://wiki.example.com", "user@example.com", "token", "12345"
        )
    assert doc is not None
    assert doc.title == "Architecture Overview"
    assert "architecture" in doc.body.lower()
    assert doc.external_id == "12345"
    assert doc.version == "3"


def test_fetch_confluence_page_strips_html_tags():
    resp_data = _confluence_response(
        "Spec",
        "<p>Some <strong>bold</strong> text and <a href='#'>link</a>.</p>",
    )
    with _patch_client(_mock_httpx_response(200, resp_data)):
        doc = fetch_confluence_page("https://wiki.example.com", "u", "t", "99")
    assert "<strong>" not in doc.body
    assert "bold" in doc.body


def test_fetch_confluence_page_404_returns_none():
    with _patch_client(_mock_httpx_response(404)):
        doc = fetch_confluence_page("https://wiki.example.com", "u", "t", "0")
    assert doc is None


def test_fetch_confluence_page_403_raises_auth_error():
    with _patch_client(_mock_httpx_response(403, text="Forbidden")):
        with pytest.raises(ContextAwareAuthError):
            fetch_confluence_page("https://wiki.example.com", "u", "bad-token", "1")


def test_fetch_confluence_page_wiki_base_url():
    """When base_url already ends with /wiki, do not insert an extra /wiki segment."""
    resp_data = _confluence_response("Page", "<p>content</p>")
    client_mock = MagicMock()
    client_mock.__enter__ = MagicMock(return_value=client_mock)
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.return_value = _mock_httpx_response(200, resp_data)

    with patch("httpx.Client", return_value=client_mock):
        fetch_confluence_page("https://example.atlassian.net/wiki", "u", "t", "42")

    called_url = client_mock.get.call_args[0][0]
    # Should be /wiki/rest/api/..., not /wiki/wiki/rest/api/...
    assert "/wiki/wiki/" not in called_url
    assert "/rest/api/content/42" in called_url
