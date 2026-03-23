"""Tests for review_decision_common helpers."""

from unittest.mock import MagicMock

import httpx

from code_review.providers.review_decision_common import (
    DEFAULT_AUTOMATED_REVIEW_BODY,
    delete_soft_fail,
    effective_review_body,
    github_style_pull_review_json,
    gitlab_note_with_submit_review_requested_changes,
)


def test_effective_review_body_uses_strip_and_default():
    assert effective_review_body("") == DEFAULT_AUTOMATED_REVIEW_BODY
    assert effective_review_body("  x  ") == "x"


def test_github_style_pull_review_json():
    j = github_style_pull_review_json("APPROVE", "hi", "abc")
    assert j == {"event": "APPROVE", "body": "hi", "commit_id": "abc"}
    j2 = github_style_pull_review_json("REQUEST_CHANGES", "", "")
    assert j2["event"] == "REQUEST_CHANGES"
    assert j2["body"] == DEFAULT_AUTOMATED_REVIEW_BODY
    assert "commit_id" not in j2


def test_gitlab_note_with_submit_review_requested_changes():
    note = gitlab_note_with_submit_review_requested_changes("Reason here")
    assert note.endswith("/submit_review requested_changes")
    assert "Reason here" in note


def test_delete_soft_fail_calls_delete_fn():
    delete_fn = MagicMock()
    delete_soft_fail(delete_fn, "https://example.com/api/approve")
    delete_fn.assert_called_once_with("https://example.com/api/approve")


def test_delete_soft_fail_ignores_safe_code_404():
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    exc = httpx.HTTPStatusError("not found", request=MagicMock(), response=mock_resp)
    delete_fn = MagicMock(side_effect=exc)
    # Should not raise
    delete_soft_fail(delete_fn, "https://example.com/api/approve")


def test_delete_soft_fail_custom_safe_codes():
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    exc = httpx.HTTPStatusError("forbidden", request=MagicMock(), response=mock_resp)

    # 403 is not in the default safe set — silently swallowed (logged as warning, no raise)
    delete_fn = MagicMock(side_effect=exc)
    delete_soft_fail(delete_fn, "https://example.com/api/approve")
    delete_fn.assert_called_once()

    # With custom safe_codes that include 403, delete_fn is still called once and returns cleanly
    delete_fn2 = MagicMock(side_effect=exc)
    delete_soft_fail(
        delete_fn2,
        "https://example.com/api/approve",
        safe_codes=frozenset({403, 404}),
    )
    delete_fn2.assert_called_once()


def test_delete_soft_fail_ignores_generic_exception():
    delete_fn = MagicMock(side_effect=RuntimeError("network error"))
    # Should not raise
    delete_soft_fail(delete_fn, "https://example.com/api/approve", log_label="test label")


def test_delete_soft_fail_logs_warning_for_unexpected_http_status():
    """Non-safe HTTP errors are swallowed with a warning (not re-raised)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    exc = httpx.HTTPStatusError("server error", request=MagicMock(), response=mock_resp)
    delete_fn = MagicMock(side_effect=exc)
    # Should not raise — logs a warning instead
    delete_soft_fail(delete_fn, "https://example.com/api/approve")
