"""Tests for GitHubIssuesFetcher (mocked HTTP)."""

from unittest.mock import MagicMock, patch

from code_review.context.github_issues import GitHubIssuesFetcher


def _make_mock_client(issue_resp, comments_resp=None):
    """Return a context-manager mock for httpx.Client."""
    mock_issue = MagicMock()
    mock_issue.json.return_value = issue_resp
    mock_issue.raise_for_status = MagicMock()

    mock_comments = MagicMock()
    mock_comments.json.return_value = comments_resp or []
    mock_comments.raise_for_status = MagicMock()

    mock_get = MagicMock(side_effect=[mock_issue, mock_comments])
    mock_enter = MagicMock()
    mock_enter.get = mock_get

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_enter)
    mock_client.__exit__ = MagicMock(return_value=False)
    return mock_client


@patch("code_review.context.github_issues.httpx.Client")
def test_fetch_issue_basic(mock_client_cls):
    issue_data = {
        "title": "Add rate limiting",
        "body": "We need to add rate limiting to the API.",
        "state": "open",
        "labels": [{"name": "enhancement"}, {"name": "backend"}],
    }
    mock_client_cls.return_value = _make_mock_client(issue_data)

    fetcher = GitHubIssuesFetcher("https://api.github.com", "tok")
    result = fetcher.fetch_issue("acme", "api", 42)

    assert "Add rate limiting" in result
    assert "We need to add rate limiting" in result
    assert "enhancement" in result
    assert "open" in result


@patch("code_review.context.github_issues.httpx.Client")
def test_fetch_issue_with_comments(mock_client_cls):
    issue_data = {
        "title": "Bug: NPE on login",
        "body": "Null pointer exception when user has no profile.",
        "state": "closed",
        "labels": [],
    }
    comments_data = [
        {"user": {"login": "alice"}, "body": "I can reproduce this."},
        {"user": {"login": "bob"}, "body": "Fixed in PR #10."},
    ]
    mock_client_cls.return_value = _make_mock_client(issue_data, comments_data)

    fetcher = GitHubIssuesFetcher("https://api.github.com", "tok")
    result = fetcher.fetch_issue("acme", "api", 5)

    assert "alice" in result
    assert "bob" in result
    assert "I can reproduce this." in result


@patch("code_review.context.github_issues.httpx.Client")
def test_fetch_issue_returns_empty_on_error(mock_client_cls):
    mock_client_cls.side_effect = Exception("network error")

    fetcher = GitHubIssuesFetcher("https://api.github.com", "tok")
    result = fetcher.fetch_issue("acme", "api", 99)

    assert result == ""


@patch("code_review.context.github_issues.httpx.Client")
def test_fetch_issue_no_body(mock_client_cls):
    issue_data = {"title": "Empty issue", "body": None, "state": "open", "labels": []}
    mock_client_cls.return_value = _make_mock_client(issue_data)

    fetcher = GitHubIssuesFetcher("https://api.github.com", "tok")
    result = fetcher.fetch_issue("acme", "api", 1)

    assert "Empty issue" in result
    # Should not crash on None body
    assert result != ""
