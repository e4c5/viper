"""Tests for JiraFetcher (mocked HTTP)."""

from unittest.mock import MagicMock, patch

from code_review.context.jira import JiraFetcher, _adf_to_text


def _make_mock_client(resp_data):
    mock_resp = MagicMock()
    mock_resp.json.return_value = resp_data
    mock_resp.raise_for_status = MagicMock()

    mock_enter = MagicMock()
    mock_enter.get = MagicMock(return_value=mock_resp)

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_enter)
    mock_client.__exit__ = MagicMock(return_value=False)
    return mock_client


@patch("code_review.context.jira.httpx.Client")
def test_fetch_ticket_basic(mock_client_cls):
    ticket_data = {
        "fields": {
            "summary": "Add OAuth2 support",
            "description": "Implement OAuth2 flow for external integrations.",
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Story"},
            "priority": {"name": "High"},
            "labels": ["auth", "backend"],
            "comment": {"comments": []},
        }
    }
    mock_client_cls.return_value = _make_mock_client(ticket_data)

    fetcher = JiraFetcher("https://company.atlassian.net", "user@x.com", "token")
    result = fetcher.fetch_ticket("PROJ-42")

    assert "Add OAuth2 support" in result
    assert "In Progress" in result
    assert "Story" in result
    assert "High" in result
    assert "auth" in result
    assert "Implement OAuth2" in result


@patch("code_review.context.jira.httpx.Client")
def test_fetch_ticket_with_comments(mock_client_cls):
    ticket_data = {
        "fields": {
            "summary": "Fix login bug",
            "description": "Users cannot log in.",
            "status": {"name": "Open"},
            "issuetype": {"name": "Bug"},
            "priority": {"name": "Critical"},
            "labels": [],
            "comment": {
                "comments": [
                    {
                        "author": {"displayName": "Alice"},
                        "body": "Reproduced on v1.2.",
                    }
                ]
            },
        }
    }
    mock_client_cls.return_value = _make_mock_client(ticket_data)

    fetcher = JiraFetcher("https://company.atlassian.net", "user@x.com", "token")
    result = fetcher.fetch_ticket("BUG-7")

    assert "Alice" in result
    assert "Reproduced on v1.2." in result


@patch("code_review.context.jira.httpx.Client")
def test_fetch_ticket_returns_empty_on_error(mock_client_cls):
    mock_client_cls.side_effect = Exception("connection refused")

    fetcher = JiraFetcher("https://company.atlassian.net", "user@x.com", "token")
    result = fetcher.fetch_ticket("PROJ-99")

    assert result == ""


def test_adf_to_text_simple():
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Hello world"}],
            }
        ],
    }
    assert "Hello world" in _adf_to_text(adf)


def test_adf_to_text_nested():
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Item one"}],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    text = _adf_to_text(adf)
    assert "Item one" in text
