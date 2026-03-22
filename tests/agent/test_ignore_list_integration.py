"""Tests for ignore list integration with manually-resolved comments (Phase 2)."""

from unittest.mock import MagicMock, patch

from code_review.diff.fingerprint import format_comment_body_with_marker
from code_review.providers.base import FileInfo, ProviderCapabilities, ReviewComment
from tests.conftest import runner_run_async_returning


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_scm_config")
@patch("code_review.runner._fingerprint_for_finding")
def test_manually_resolved_comment_does_not_block_changed_code(
    mock_fingerprint_for_finding,
    mock_get_scm_config,
    mock_get_llm_config,
    mock_get_context_window,
):
    """
    When a comment was manually resolved, the ignore logic uses the fingerprint
    (code anchor), so if the fingerprint changes for new code, the finding is
    allowed to post even if the body text is identical.
    """
    from code_review.runner import run_review

    mock_get_scm_config.return_value = MagicMock(provider="gitea", url="https://x.com", token="x")
    mock_get_llm_config.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    mock_get_context_window.return_value = 1_000_000

    provider = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        resolvable_comments=False, supports_suggestions=False
    )
    provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    provider.get_pr_diff.return_value = "diff"
    provider.get_file_content.return_value = "content"

    # Existing comment is manually resolved, with fingerprint "old-fp" and body text
    # that matches the body which will be generated for the new finding.
    existing_body = format_comment_body_with_marker(
        "[Medium] Use a constant.", fingerprint="old-fp", version="1", run_id="run-1"
    )
    existing = [
        ReviewComment(
            id="c-1",
            path="foo.py",
            line=1,
            body=existing_body,
            resolved=True,
        )
    ]
    provider.get_existing_review_comments.return_value = existing

    # New finding will use the same human-visible message, but we force a different
    # fingerprint "new-fp" to simulate materially changed surrounding code.
    mock_fingerprint_for_finding.return_value = "new-fp"

    findings_json = """
    [
        {
            "path": "foo.py",
            "line": 1,
            "severity": "medium",
            "code": "use-const",
            "message": "Use a constant."
        }
    ]
    """
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=findings_json)]
    mock_runner_instance = MagicMock()
    mock_runner_instance.run_async = runner_run_async_returning([mock_event])

    provider.post_review_comments = MagicMock()
    provider.post_pr_summary_comment = MagicMock()

    with (
        patch("code_review.runner.get_provider", return_value=provider),
        patch("google.adk.runners.Runner", return_value=mock_runner_instance),
    ):
        to_post = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    # The manually-resolved comment should NOT suppress the new finding, because
    # the fingerprint changed (new-fp vs old-fp).
    assert len(to_post) == 1
    provider.post_review_comments.assert_called_once()
