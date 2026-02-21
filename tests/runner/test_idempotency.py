"""Tests for idempotency key and skip-when-already-run (Phase 2)."""

from unittest.mock import MagicMock, patch

from code_review.runner import (
    _build_idempotency_key,
    _idempotency_key_seen_in_comments,
    run_review,
)


@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_scm_config")
def test_build_idempotency_key_format(mock_scm, mock_llm):
    mock_scm.return_value = MagicMock(
        provider="gitea", url="https://gitea.example.com", token="x"
    )
    mock_llm.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    key = _build_idempotency_key(
        mock_scm.return_value, mock_llm.return_value, "o", "r", 1, "abc123"
    )
    assert "gitea/o/r/pr/1/head/abc123" in key
    assert "agent/" in key
    assert "config/" in key


def test_idempotency_key_seen_in_comments():
    comments_no_run = [{"path": "a.py", "body": "Hello"}]
    assert _idempotency_key_seen_in_comments(comments_no_run, "my-run-id") is False
    body_with_run = "<!-- code-review-agent:fingerprint=x;version=0.1.0;run=my-run-id -->\n\nDone."
    comments_with_run = [{"path": "a.py", "body": body_with_run}]
    assert _idempotency_key_seen_in_comments(comments_with_run, "my-run-id") is True
    assert _idempotency_key_seen_in_comments(comments_with_run, "other-run") is False


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_skips_when_idempotency_key_seen(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """When existing comment contains run=<current_key>, run_review returns [] without running agent."""
    from code_review.providers.base import FileInfo

    mock_get_scm_config.return_value = MagicMock(
        provider="gitea", url="https://x.com", token="x"
    )
    mock_get_llm_config.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    provider = MagicMock()
    provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    provider.get_pr_diff.return_value = "diff"
    provider.get_file_content.return_value = "x"
    run_id = _build_idempotency_key(
        mock_get_scm_config.return_value,
        mock_get_llm_config.return_value,
        "o", "r", 1, "abc123",
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
