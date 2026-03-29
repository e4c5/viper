"""Tests for idempotency key and skip-when-already-run (Phase 2)."""

from unittest.mock import MagicMock, patch

from code_review.diff.fingerprint import format_comment_body_with_marker
from code_review.providers.base import FileInfo, ProviderCapabilities
from code_review.runner import (
    AGENT_VERSION,
    _build_idempotency_key,
    _idempotency_key_seen_in_comments,
    run_review,
)
from tests.conftest import sample_unified_diff


@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_scm_config")
def test_build_idempotency_key_format(mock_scm, mock_llm):
    mock_scm.return_value = MagicMock(provider="gitea", url="https://gitea.example.com", token="x")
    mock_llm.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    key = _build_idempotency_key(
        mock_scm.return_value, mock_llm.return_value, "o", "r", 1, "abc123"
    )
    assert "gitea/o/r/pr/1/head/abc123" in key
    assert "/base/" in key
    assert "agent/" in key
    assert "config/" in key


@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_scm_config")
def test_build_idempotency_key_includes_incremental_base(mock_scm, mock_llm):
    mock_scm.return_value = MagicMock(provider="gitea", url="https://gitea.example.com", token="x")
    mock_llm.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")

    key = _build_idempotency_key(
        mock_scm.return_value, mock_llm.return_value, "o", "r", 1, "abc123", "base456"
    )

    assert "head/abc123/base/base456" in key


def test_idempotency_key_seen_in_comments():
    comments_no_run = [{"path": "a.py", "body": "Hello"}]
    assert _idempotency_key_seen_in_comments(comments_no_run, "my-run-id") is False
    body_with_run = "<!-- code-review-agent:fingerprint=x;version=0.1.0;run=my-run-id -->\n\nDone."
    comments_with_run = [{"path": "a.py", "body": body_with_run}]
    assert _idempotency_key_seen_in_comments(comments_with_run, "my-run-id") is True
    assert _idempotency_key_seen_in_comments(comments_with_run, "other-run") is False


@patch("code_review.orchestration_deps.get_context_window")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_scm_config")
def test_run_review_skips_when_idempotency_key_seen(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """
    When existing comment contains run=<current_key>, run_review returns []
    without running the agent.
    """
    from code_review.providers.base import FileInfo

    mock_get_scm_config.return_value = MagicMock(provider="gitea", url="https://x.com", token="x")
    mock_get_llm_config.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    provider = MagicMock()
    provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    provider.get_pr_diff.return_value = sample_unified_diff("foo.py")
    provider.get_file_content.return_value = "x"
    run_id = _build_idempotency_key(
        mock_get_scm_config.return_value,
        mock_get_llm_config.return_value,
        "o",
        "r",
        1,
        "abc123",
    )
    body_with_run = f"<!-- code-review-agent:fingerprint=x;version=0.1.0;run={run_id} -->\n\nOld."
    provider.get_existing_review_comments.return_value = [
        MagicMock(
            path="foo.py",
            body=body_with_run,
            model_dump=lambda: {"path": "foo.py", "body": body_with_run},
        )
    ]
    mock_get_provider.return_value = provider
    mock_get_context_window.return_value = 1_000_000

    to_post = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    assert len(to_post) == 0
    provider.post_review_comments.assert_not_called()


@patch("code_review.orchestration_deps.get_context_window")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_scm_config")
def test_run_review_skips_when_omit_marker_pr_summary_contains_run_id(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """Bitbucket-style providers store run id on PR summary comments (marker at end)."""
    mock_get_scm_config.return_value = MagicMock(provider="gitea", url="https://x.com", token="x")
    mock_get_llm_config.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    provider = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        resolvable_comments=False,
        supports_suggestions=False,
        omit_fingerprint_marker_in_body=True,
        embed_agent_marker_as_commonmark_linkref=True,
    )
    provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    provider.get_pr_diff.return_value = sample_unified_diff("foo.py")
    provider.get_file_content.return_value = "x"
    run_id = _build_idempotency_key(
        mock_get_scm_config.return_value,
        mock_get_llm_config.return_value,
        "o",
        "r",
        1,
        "abc123",
    )
    body = format_comment_body_with_marker(
        "**Viper** finished.\n\nSummary.",
        "",
        AGENT_VERSION,
        run_id=run_id,
        marker_at_end=True,
        use_commonmark_linkref=True,
    )
    provider.get_existing_review_comments.return_value = [
        MagicMock(
            path="",
            body=body,
            model_dump=lambda: {"path": "", "body": body},
        )
    ]
    mock_get_provider.return_value = provider
    mock_get_context_window.return_value = 1_000_000

    to_post = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    assert len(to_post) == 0
    provider.post_review_comments.assert_not_called()
