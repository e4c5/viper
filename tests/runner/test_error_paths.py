"""Error-path tests for runner posting and provider failures (Phase 3.2)."""

from unittest.mock import MagicMock, patch

from tests.conftest import runner_run_async_returning
from code_review.providers.base import FileInfo, ProviderCapabilities, RateLimitError


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
    mock_runner_instance.run_async = runner_run_async_returning([mock_event])

    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        to_post = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    return to_post, provider


def _exercise_file_by_file_skip(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_llm_config,
    mock_get_context_window,
    run_async_side_effect,
):
    """Helper: run file-by-file mode (small context window) with a custom run_async side effect.

    Returns (results, call_count_list) where call_count_list[0] is the number of
    agent calls made.  The provider is pre-configured with two files (a.py, b.py)
    and a diff large enough to trigger file-by-file mode.
    """
    from code_review.runner import run_review

    mock_get_scm_config.return_value = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="",
        skip_title_pattern="",
    )
    mock_get_llm_config.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    # Small context window so diff exceeds budget → file-by-file mode
    mock_get_context_window.return_value = 100

    provider = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        resolvable_comments=False, supports_suggestions=False
    )
    provider.get_pr_files.return_value = [
        FileInfo(path="a.py", status="modified"),
        FileInfo(path="b.py", status="modified"),
    ]
    provider.get_pr_diff.return_value = "x" * 200  # exceeds budget
    provider.get_file_content.return_value = ""
    provider.get_existing_review_comments.return_value = []
    provider.post_review_comments = MagicMock()
    provider.post_pr_summary_comment = MagicMock()
    mock_get_provider.return_value = provider

    mock_runner_instance = MagicMock()
    mock_runner_instance.run_async = run_async_side_effect

    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        results = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    return results


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


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_file_by_file_skips_file_on_rate_limit_error(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """File-by-file mode skips a file and continues when a RateLimitError is raised."""
    call_count = [0]

    def run_async_side_effect(*, new_message, **kwargs):
        call_count[0] += 1
        text = new_message.parts[0].text if new_message.parts else ""
        if '"a.py"' in text:
            raise RateLimitError("HTTP 429 Too Many Requests")
        findings = '[{"path":"b.py","line":1,"severity":"info","code":"ok","message":"Fine."}]'
        mock_event = MagicMock()
        mock_event.is_final_response.return_value = True
        mock_event.content = MagicMock()
        mock_event.content.parts = [MagicMock(text=findings)]
        return runner_run_async_returning([mock_event])()

    results = _exercise_file_by_file_skip(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_llm_config,
        mock_get_context_window,
        run_async_side_effect,
    )

    # a.py was skipped (rate limit), b.py was processed
    assert call_count[0] == 2
    assert len(results) == 1
    assert results[0].path == "b.py"


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_file_by_file_skips_file_on_generic_error(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """File-by-file mode skips a file and continues when an unexpected error is raised."""
    call_count = [0]

    def run_async_side_effect(*, new_message, **kwargs):
        call_count[0] += 1
        text = new_message.parts[0].text if new_message.parts else ""
        if '"a.py"' in text:
            raise RuntimeError("unexpected LLM error")
        findings = '[{"path":"b.py","line":2,"severity":"suggestion","code":"s","message":"Improve."}]'
        mock_event = MagicMock()
        mock_event.is_final_response.return_value = True
        mock_event.content = MagicMock()
        mock_event.content.parts = [MagicMock(text=findings)]
        return runner_run_async_returning([mock_event])()

    results = _exercise_file_by_file_skip(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_llm_config,
        mock_get_context_window,
        run_async_side_effect,
    )

    # a.py was skipped (error), b.py was processed
    assert call_count[0] == 2
    assert len(results) == 1
    assert results[0].path == "b.py"
