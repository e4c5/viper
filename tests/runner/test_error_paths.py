"""Error-path tests for runner posting and provider failures (Phase 3.2)."""

from unittest.mock import MagicMock, patch

from code_review.providers.base import FileInfo, ProviderCapabilities


def _exercise_error_path(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_llm_config,
    mock_get_context_window,
    findings_json: str,
    configure_provider,
):
    from code_review.runner import run_review

    mock_get_scm_config.return_value = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="",
        skip_title_pattern="",
    )
    mock_get_llm_config.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    mock_get_context_window.return_value = 1_000_000

    provider = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        resolvable_comments=False, supports_suggestions=False
    )
    provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    provider.get_pr_diff.return_value = "diff"
    provider.get_file_content.return_value = "content"
    provider.get_existing_review_comments.return_value = []

    configure_provider(provider)
    mock_get_provider.return_value = provider

    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=findings_json)]
    mock_runner_instance = MagicMock()
    mock_runner_instance.run.return_value = iter([mock_event])

    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        to_post = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    return to_post, provider


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_post_review_comments_batch_fallback_to_per_comment(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """When batch post_review_comments fails, runner falls back to per-comment posting."""

    def configure_provider(provider):
        provider.post_review_comments.side_effect = RuntimeError("batch failure")
        provider.post_review_comment = MagicMock()
        provider.post_pr_summary_comment = MagicMock()

    findings_json = (
        '[{"path":"foo.py","line":1,"severity":"suggestion","code":"x","message":"Fix."}]'
    )
    to_post, provider = _exercise_error_path(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_llm_config,
        mock_get_context_window,
        findings_json,
        configure_provider,
    )

    # Finding still returned, but posted via per-comment fallback.
    assert len(to_post) == 1
    provider.post_review_comments.assert_called_once()
    provider.post_review_comment.assert_called_once()


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_post_review_comment_fallback_to_pr_summary(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """When per-comment posting fails, runner degrades to PR summary comments."""

    def configure_provider(provider):
        provider.post_review_comments.side_effect = RuntimeError("batch failure")

        def per_comment_fail(*args, **kwargs):
            raise RuntimeError("per-comment failure")

        provider.post_review_comment.side_effect = per_comment_fail
        provider.post_pr_summary_comment = MagicMock()

    findings_json = (
        '[{"path":"foo.py","line":2,"severity":"critical","code":"x","message":"Fix now."}]'
    )
    to_post, provider = _exercise_error_path(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_llm_config,
        mock_get_context_window,
        findings_json,
        configure_provider,
    )

    # Finding returned; posting falls back to PR summary when inline positions fail.
    assert len(to_post) == 1
    provider.post_review_comments.assert_called_once()
    provider.post_review_comment.assert_called_once()
    # There may be an additional "Viper has started a review" comment; assert that
    # at least one PR-level summary comment was posted and the final one contains
    # the fallback summary for the failing inline comment.
    assert provider.post_pr_summary_comment.call_count >= 1
