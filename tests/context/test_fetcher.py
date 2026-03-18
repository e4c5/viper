"""Tests for ContextFetcher."""

from unittest.mock import MagicMock, patch

from code_review.context.fetcher import ContextFetcher


def _make_config(
    enabled=True,
    github_issues_enabled=True,
    jira_enabled=False,
    confluence_enabled=False,
    jira_url=None,
    jira_email=None,
    jira_token=None,
    confluence_url=None,
    confluence_email=None,
    confluence_token=None,
    max_context_tokens=20_000,
    jira_project_keys=None,
):
    cfg = MagicMock()
    cfg.enabled = enabled
    cfg.github_issues_enabled = github_issues_enabled
    cfg.jira_enabled = jira_enabled
    cfg.confluence_enabled = confluence_enabled
    cfg.jira_url = jira_url
    cfg.jira_email = jira_email
    cfg.jira_token = MagicMock(get_secret_value=lambda: jira_token) if jira_token else None
    cfg.confluence_url = confluence_url
    cfg.confluence_email = confluence_email
    cfg.confluence_token = (
        MagicMock(get_secret_value=lambda: confluence_token) if confluence_token else None
    )
    cfg.max_context_tokens = max_context_tokens
    cfg.jira_project_keys = jira_project_keys
    return cfg


class TestContextFetcherDisabled:
    def test_returns_empty_when_disabled(self):
        cfg = _make_config(enabled=False)
        fetcher = ContextFetcher("https://api.github.com", "tok", cfg, "owner", "repo")
        result = fetcher.build_context("See #5", "Fix stuff", [])
        assert result == ""

    def test_returns_empty_when_no_refs(self):
        cfg = _make_config(enabled=True)
        fetcher = ContextFetcher("https://api.github.com", "tok", cfg, "owner", "repo")
        result = fetcher.build_context("Just a plain description", "Fix stuff", [])
        assert result == ""


class TestContextFetcherGitHubIssues:
    @patch("code_review.context.fetcher.ContextFetcher._fetch_github_issue")
    def test_builds_context_block(self, mock_fetch):
        mock_fetch.return_value = "GitHub Issue #5 (open): Fix login bug\nSome details."
        cfg = _make_config(enabled=True, github_issues_enabled=True)
        fetcher = ContextFetcher("https://api.github.com", "tok", cfg, "owner", "repo")
        result = fetcher.build_context("Fixes #5", "Fix login bug", [])
        assert "<context>" in result
        assert "Fix login bug" in result
        assert "</context>" in result

    @patch("code_review.context.fetcher.ContextFetcher._fetch_github_issue")
    def test_returns_empty_when_fetch_returns_empty(self, mock_fetch):
        mock_fetch.return_value = ""
        cfg = _make_config(enabled=True, github_issues_enabled=True)
        fetcher = ContextFetcher("https://api.github.com", "tok", cfg, "owner", "repo")
        result = fetcher.build_context("Fixes #5", "title", [])
        assert result == ""

    @patch("code_review.context.fetcher.ContextFetcher._fetch_github_issue")
    def test_multiple_issues_combined(self, mock_fetch):
        mock_fetch.side_effect = lambda ref: f"Issue {ref.identifier}"
        cfg = _make_config(enabled=True, github_issues_enabled=True)
        fetcher = ContextFetcher("https://api.github.com", "tok", cfg, "owner", "repo")
        result = fetcher.build_context("Fixes #1 and #2", "title", [])
        assert "owner/repo/1" in result
        assert "owner/repo/2" in result


class TestContextFetcherTokenBudget:
    @patch("code_review.context.fetcher.ContextFetcher._fetch_github_issue")
    def test_truncates_to_budget(self, mock_fetch):
        # Each call returns 1000-char content; budget is only 100 tokens = 400 chars
        mock_fetch.return_value = "x" * 1000
        cfg = _make_config(enabled=True, max_context_tokens=100)
        fetcher = ContextFetcher("https://api.github.com", "tok", cfg, "owner", "repo")
        result = fetcher.build_context("Fixes #1", "title", [])
        # Should be truncated; total chars should be within budget (+overhead for tags)
        assert "truncated" in result or len(result) < 1000


class TestContextFetcherCommitMessages:
    @patch("code_review.context.fetcher.ContextFetcher._fetch_github_issue")
    def test_extracts_refs_from_commit_messages(self, mock_fetch):
        mock_fetch.return_value = "Issue content"
        cfg = _make_config(enabled=True, github_issues_enabled=True)
        fetcher = ContextFetcher("https://api.github.com", "tok", cfg, "acme", "app")
        result = fetcher.build_context(
            pr_description="",
            pr_title="",
            commit_messages=["Fixes #7: add rate limiting"],
        )
        assert "<context>" in result
        mock_fetch.assert_called_once()
        call_ref = mock_fetch.call_args[0][0]
        assert call_ref.identifier == "acme/app/7"
