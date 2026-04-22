"""Unit tests for context fetchers (mocked HTTP via httpx)."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from github.GithubException import GithubException, UnknownObjectException

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
    resp.headers = {}
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


def _patch_github_client(*, issue=None, side_effect=None):
    client = MagicMock()
    client.get_issue.return_value = issue
    if side_effect is not None:
        client.get_issue.side_effect = side_effect
    return patch("code_review.context.fetchers._build_github_api_client", return_value=client)


# ---------------------------------------------------------------------------
# fetch_github_issue
# ---------------------------------------------------------------------------


def test_fetch_github_issue_happy_path():
    issue = MagicMock(
        title="Bug: crash on login",
        body="Steps to reproduce…",
        state="open",
        labels=[SimpleNamespace(name="bug"), SimpleNamespace(name="urgent")],
        html_url="https://github.com/org/repo/issues/42",
        id=1234,
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    with _patch_github_client(issue=issue):
        doc = fetch_github_issue("https://api.github.com", "tok", "org", "repo", "42")
    assert doc is not None
    assert doc.title == "Bug: crash on login"
    assert "bug" in doc.body
    assert doc.external_id == "org/repo#42"
    assert doc.external_updated_at == "2024-01-01T00:00:00Z"


def test_fetch_github_issue_404_returns_none():
    with _patch_github_client(
        side_effect=UnknownObjectException(404, {"message": "Not Found"}, None)
    ):
        doc = fetch_github_issue("https://api.github.com", "tok", "org", "repo", "99")
    assert doc is None


def test_fetch_github_issue_401_raises_auth_error():
    with _patch_github_client(side_effect=GithubException(401, {"message": "Unauthorized"})):
        with pytest.raises(ContextAwareAuthError):
            fetch_github_issue("https://api.github.com", "bad-tok", "org", "repo", "1")


def test_fetch_github_issue_403_raises_auth_error():
    with _patch_github_client(side_effect=GithubException(403, {"message": "Forbidden"})):
        with pytest.raises(ContextAwareAuthError):
            fetch_github_issue("https://api.github.com", "tok", "org", "repo", "1")


def test_fetch_github_issue_500_raises_fatal():
    with _patch_github_client(
        side_effect=GithubException(500, {"message": "Internal Server Error"})
    ):
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


def test_fetch_jira_issue_uses_top_level_atlassian_url():
    data = {
        "id": "10001",
        "fields": {
            "summary": "Use top-level URL",
            "description": None,
            "issuetype": {"name": "Task"},
            "status": {"name": "Open"},
            "updated": None,
        },
    }
    client_mock = MagicMock()
    client_mock.__enter__ = MagicMock(return_value=client_mock)
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.return_value = _mock_httpx_response(200, data)

    with patch("httpx.Client", return_value=client_mock):
        fetch_jira_issue("https://example.atlassian.net", "u", "t", "KAN-6")

    assert client_mock.get.call_args.args[0] == (
        "https://example.atlassian.net/rest/api/3/issue/KAN-6"
    )


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


def test_fetch_jira_issue_appends_remote_links_when_requested():
    data = {
        "id": "10002",
        "fields": {
            "summary": "Review linked spec",
            "description": "Related Confluence Page",
            "issuetype": {"name": "Task"},
            "status": {"name": "Open"},
            "updated": None,
        },
    }
    remote_links = [
        {
            "object": {
                "title": "KAN-9 implementation notes",
                "url": "https://example.atlassian.net/wiki/spaces/ENG/pages/555/KAN-9",
            }
        }
    ]
    client_mock = _make_multi_response_client(
        [_mock_httpx_response(200, data), _mock_httpx_response(200, remote_links)]
    )

    with patch("httpx.Client", return_value=client_mock):
        doc = fetch_jira_issue(
            "https://example.atlassian.net",
            "user@example.com",
            "token",
            "KAN-9",
            include_remote_links=True,
        )

    assert doc is not None
    assert "Related Confluence Page" in doc.body
    assert "Remote links:" in doc.body
    assert "https://example.atlassian.net/wiki/spaces/ENG/pages/555/KAN-9" in doc.body
    assert doc.metadata["jira_remote_links_included"] is True
    assert doc.metadata["jira_remote_link_count"] == 1
    assert client_mock.get.call_args_list[1].args[0] == (
        "https://example.atlassian.net/rest/api/3/issue/KAN-9/remotelink"
    )


def test_fetch_jira_issue_skips_remote_links_on_auth_error():
    data = {
        "id": "10002",
        "fields": {
            "summary": "Review linked spec",
            "description": "Related Confluence Page",
            "issuetype": {"name": "Task"},
            "status": {"name": "Open"},
            "updated": None,
        },
    }
    client_mock = _make_multi_response_client(
        [_mock_httpx_response(200, data), _mock_httpx_response(401, text="Unauthorized")]
    )

    with patch("httpx.Client", return_value=client_mock):
        doc = fetch_jira_issue(
            "https://example.atlassian.net",
            "user@example.com",
            "token",
            "KAN-9",
            include_remote_links=True,
        )

    assert doc is not None
    assert "Related Confluence Page" in doc.body
    assert "Remote links:" not in doc.body
    assert doc.metadata["jira_remote_links_included"] is False
    assert doc.metadata["jira_remote_link_count"] == 0


def test_fetch_jira_issue_skips_remote_links_on_invalid_json():
    data = {
        "id": "10002",
        "fields": {
            "summary": "Review linked spec",
            "description": "Related Confluence Page",
            "issuetype": {"name": "Task"},
            "status": {"name": "Open"},
            "updated": None,
        },
    }
    invalid_json_response = _mock_httpx_response(200, text="not json")
    invalid_json_response.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
    client_mock = _make_multi_response_client(
        [_mock_httpx_response(200, data), invalid_json_response]
    )

    with patch("httpx.Client", return_value=client_mock):
        doc = fetch_jira_issue(
            "https://example.atlassian.net",
            "user@example.com",
            "token",
            "KAN-9",
            include_remote_links=True,
        )

    assert doc is not None
    assert "Related Confluence Page" in doc.body
    assert "Remote links:" not in doc.body
    assert doc.metadata["jira_remote_links_included"] is False
    assert doc.metadata["jira_remote_link_count"] == 0


def test_fetch_jira_issue_preserves_confluence_link_from_adf_description():
    data = {
        "id": "10002",
        "fields": {
            "summary": "Review linked spec",
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Related Confluence Page"}],
                    },
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "inlineCard",
                                "attrs": {
                                    "url": (
                                        "https://example.atlassian.net/wiki/spaces/ENG/pages/"
                                        "555/KAN-9"
                                    )
                                },
                            }
                        ],
                    },
                ],
            },
            "issuetype": {"name": "Task"},
            "status": {"name": "Open"},
            "updated": None,
        },
    }

    with _patch_client(_mock_httpx_response(200, data)):
        doc = fetch_jira_issue(
            "https://example.atlassian.net", "user@example.com", "token", "KAN-9"
        )

    assert doc is not None
    assert "Related Confluence Page" in doc.body
    assert "https://example.atlassian.net/wiki/spaces/ENG/pages/555/KAN-9" in doc.body


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


def test_fetch_confluence_page_uses_top_level_atlassian_url():
    resp_data = _confluence_response("Page", "<p>content</p>")
    client_mock = MagicMock()
    client_mock.__enter__ = MagicMock(return_value=client_mock)
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.return_value = _mock_httpx_response(200, resp_data)

    with patch("httpx.Client", return_value=client_mock):
        fetch_confluence_page("https://example.atlassian.net", "u", "t", "42")

    assert client_mock.get.call_args.args[0] == (
        "https://example.atlassian.net/wiki/rest/api/content/42"
    )


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


def test_fetch_confluence_page_500_raises_fatal():
    with _patch_client(_mock_httpx_response(500, text="Server Error")):
        with pytest.raises(ContextAwareFatalError):
            fetch_confluence_page("https://wiki.example.com", "u", "t", "1")


# ---------------------------------------------------------------------------
# fetch_gitlab_issue — additional cases
# ---------------------------------------------------------------------------


def test_fetch_gitlab_issue_500_raises_fatal():
    with _patch_client(_mock_httpx_response(500, text="Server Error")):
        with pytest.raises(ContextAwareFatalError):
            fetch_gitlab_issue("https://gitlab.com/api/v4", "tok", "group/repo", "1")


# ---------------------------------------------------------------------------
# fetch_jira_issue — additional cases
# ---------------------------------------------------------------------------


def _make_multi_response_client(responses):
    """Client mock that returns successive responses for each .get() call."""
    client_mock = MagicMock()
    client_mock.__enter__ = MagicMock(return_value=client_mock)
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.side_effect = responses
    return client_mock


def test_fetch_jira_issue_400_retries_without_extra_fields():
    """When 400 returned with extra_fields, retries with base fields and succeeds."""
    data = {
        "id": "10003",
        "fields": {
            "summary": "Retry success",
            "description": "Plain text.",
            "issuetype": {"name": "Task"},
            "status": {"name": "Done"},
            "updated": "2024-01-01T00:00:00.000+0000",
        },
    }
    bad_resp = _mock_httpx_response(400, text="Field not found")
    ok_resp = _mock_httpx_response(200, data)

    client_mock = _make_multi_response_client([bad_resp, ok_resp])
    with patch("httpx.Client", return_value=client_mock):
        doc = fetch_jira_issue(
            "https://jira.example.com",
            "u",
            "t",
            "PROJ-1",
            extra_fields=["customfield_99999"],
        )
    assert doc is not None
    assert doc.title == "Retry success"
    assert client_mock.get.call_count == 2


def test_fetch_jira_issue_500_raises_fatal():
    with _patch_client(_mock_httpx_response(500, text="Internal Error")):
        with pytest.raises(ContextAwareFatalError):
            fetch_jira_issue("https://jira.example.com", "u", "t", "PROJ-1")


def test_fetch_jira_issue_non_json_response_raises_context_error():
    resp = _mock_httpx_response(200, text="<html>login</html>")
    resp.headers = {"content-type": "text/html"}
    resp.json.side_effect = json.JSONDecodeError("Expecting value", resp.text, 0)

    with _patch_client(resp):
        with pytest.raises(ContextAwareFatalError, match="Jira issue returned non-JSON"):
            fetch_jira_issue("https://jira.example.com", "u", "t", "PROJ-1")


def test_fetch_jira_issue_with_extra_fields():
    """Extra fields are appended to the fields query parameter."""
    data = {
        "id": "10004",
        "fields": {
            "summary": "Extra fields test",
            "description": None,
            "issuetype": {"name": "Epic"},
            "status": {"name": "Open"},
            "updated": None,
            "customfield_12345": "custom value",
        },
    }
    client_mock = MagicMock()
    client_mock.__enter__ = MagicMock(return_value=client_mock)
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.return_value = _mock_httpx_response(200, data)

    with patch("httpx.Client", return_value=client_mock):
        doc = fetch_jira_issue(
            "https://jira.example.com",
            "u",
            "t",
            "PROJ-2",
            extra_fields=["customfield_12345"],
        )
    assert doc is not None
    assert "customfield_12345" in doc.body
    assert "custom value" in doc.body
    # Verify extra field was requested
    call_kwargs = client_mock.get.call_args[1]
    assert "customfield_12345" in call_kwargs["params"]["fields"]


# ---------------------------------------------------------------------------
# _strip_html_to_text
# ---------------------------------------------------------------------------


def test_strip_html_to_text_empty():
    assert _strip_html_to_text("") == ""


def test_strip_html_to_text_strips_tags():
    result = _strip_html_to_text("<p>Hello <b>world</b></p>")
    assert "Hello" in result
    assert "world" in result
    assert "<" not in result


def test_strip_html_to_text_decodes_entities():
    # &amp; is decoded to &
    result = _strip_html_to_text("&amp; more text")
    assert "&" in result


# ---------------------------------------------------------------------------
# _adf_to_plain
# ---------------------------------------------------------------------------


def test_adf_to_plain_non_dict_returns_empty():
    assert _adf_to_plain("not a dict") == ""
    assert _adf_to_plain(None) == ""
    assert _adf_to_plain(42) == ""


def test_adf_to_plain_text_node():
    node = {"type": "text", "text": "hello"}
    assert _adf_to_plain(node) == "hello"


def test_adf_to_plain_paragraph_with_text():
    node = {
        "type": "paragraph",
        "content": [{"type": "text", "text": "paragraph content"}],
    }
    result = _adf_to_plain(node)
    assert "paragraph content" in result


def test_adf_to_plain_nested_doc():
    node = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "first"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "second"}],
            },
        ],
    }
    result = _adf_to_plain(node)
    assert "first" in result
    assert "second" in result


def test_adf_to_plain_preserves_text_link_mark_href():
    node = {
        "type": "text",
        "text": "Related Confluence Page",
        "marks": [
            {
                "type": "link",
                "attrs": {
                    "href": "https://example.atlassian.net/wiki/spaces/ENG/pages/555/Spec"
                },
            }
        ],
    }

    result = _adf_to_plain(node)

    assert "Related Confluence Page" in result
    assert "https://example.atlassian.net/wiki/spaces/ENG/pages/555/Spec" in result


def test_adf_to_plain_preserves_inline_card_url():
    node = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Related Confluence Page"}],
            },
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "inlineCard",
                        "attrs": {
                            "url": "https://example.atlassian.net/wiki/spaces/ENG/pages/555/Spec"
                        },
                    }
                ],
            },
        ],
    }

    result = _adf_to_plain(node)

    assert "Related Confluence Page" in result
    assert "https://example.atlassian.net/wiki/spaces/ENG/pages/555/Spec" in result


# ---------------------------------------------------------------------------
# fetch_reference dispatcher
# ---------------------------------------------------------------------------


def _make_fetch_cfg(**overrides):
    from code_review.context.fetchers import FetchReferenceConfig

    defaults = {
        "github_api_base": "https://api.github.com",
        "github_token": "tok",
        "gitlab_api_base": "https://gitlab.com/api/v4",
        "gitlab_token": "tok",
        "jira_base": "https://jira.example.com",
        "confluence_base": "https://wiki.example.com",
        "atlassian_email": "u@e.com",
        "atlassian_token": "tok",
        "ctx_github_enabled": True,
        "ctx_gitlab_enabled": True,
        "ctx_jira_enabled": True,
        "ctx_confluence_enabled": True,
        "jira_extra_fields": (),
    }
    defaults.update(overrides)
    return FetchReferenceConfig(**defaults)


def _make_ref(ref_type, external_id):
    return ContextReference(ref_type=ref_type, external_id=external_id, display=external_id)


def test_fetch_reference_github_disabled_returns_none():
    ref = _make_ref(ReferenceType.GITHUB_ISSUE, "org/repo#1")
    cfg = _make_fetch_cfg(ctx_github_enabled=False)
    result = fetch_reference(ref, cfg=cfg)
    assert result is None


def test_fetch_reference_jira_disabled_returns_none():
    ref = _make_ref(ReferenceType.JIRA, "PROJ-1")
    cfg = _make_fetch_cfg(ctx_jira_enabled=False)
    result = fetch_reference(ref, cfg=cfg)
    assert result is None


def test_fetch_reference_confluence_disabled_returns_none():
    ref = _make_ref(ReferenceType.CONFLUENCE, "12345")
    cfg = _make_fetch_cfg(ctx_confluence_enabled=False)
    result = fetch_reference(ref, cfg=cfg)
    assert result is None


def test_fetch_reference_gitlab_disabled_returns_none():
    ref = _make_ref(ReferenceType.GITLAB_ISSUE, "group/repo#1")
    cfg = _make_fetch_cfg(ctx_gitlab_enabled=False)
    result = fetch_reference(ref, cfg=cfg)
    assert result is None


def test_fetch_reference_github_network_error_returns_none():
    import httpx

    ref = _make_ref(ReferenceType.GITHUB_ISSUE, "org/repo#1")
    cfg = _make_fetch_cfg()
    with patch(
        "code_review.context.fetchers.fetch_github_issue",
        side_effect=httpx.ConnectError("timeout"),
    ):
        result = fetch_reference(ref, cfg=cfg)
    assert result is None


def test_fetch_reference_auth_error_propagates():
    ref = _make_ref(ReferenceType.GITHUB_ISSUE, "org/repo#1")
    cfg = _make_fetch_cfg()
    with patch(
        "code_review.context.fetchers.fetch_github_issue",
        side_effect=ContextAwareAuthError("401"),
    ):
        with pytest.raises(ContextAwareAuthError):
            fetch_reference(ref, cfg=cfg)


def test_fetch_reference_fatal_error_is_downgraded_to_none():
    ref = _make_ref(ReferenceType.GITHUB_ISSUE, "org/repo#1")
    cfg = _make_fetch_cfg()
    with patch(
        "code_review.context.fetchers.fetch_github_issue",
        side_effect=ContextAwareFatalError("500"),
    ):
        result = fetch_reference(ref, cfg=cfg)
    assert result is None


def test_fetch_reference_dispatches_to_jira():
    ref = _make_ref(ReferenceType.JIRA, "PROJ-99")
    cfg = _make_fetch_cfg()
    with patch("code_review.context.fetchers.fetch_jira_issue", return_value=None) as mock_jira:
        fetch_reference(ref, cfg=cfg)
    mock_jira.assert_called_once()
    _, kwargs = mock_jira.call_args
    assert kwargs.get("key") == "PROJ-99" or mock_jira.call_args[0][3] == "PROJ-99"
    assert kwargs["include_remote_links"] is True


def test_fetch_reference_skips_jira_remote_links_when_confluence_disabled():
    ref = _make_ref(ReferenceType.JIRA, "PROJ-99")
    cfg = _make_fetch_cfg(ctx_confluence_enabled=False)
    with patch("code_review.context.fetchers.fetch_jira_issue", return_value=None) as mock_jira:
        fetch_reference(ref, cfg=cfg)
    _, kwargs = mock_jira.call_args
    assert kwargs["include_remote_links"] is False


def test_fetch_reference_dispatches_to_confluence():
    ref = _make_ref(ReferenceType.CONFLUENCE, "99999")
    cfg = _make_fetch_cfg()
    with patch(
        "code_review.context.fetchers.fetch_confluence_page", return_value=None
    ) as mock_conf:
        fetch_reference(ref, cfg=cfg)
    mock_conf.assert_called_once()
