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


def _build_file_by_file_run_async_side_effect(call_count, error_factory, findings: str):
    """Factory for run_async side effects used in file-by-file skip tests."""

    def run_async_side_effect(*, new_message, **kwargs):
        call_count[0] += 1
        text = new_message.parts[0].text if new_message.parts else ""
        if '"a.py"' in text:
            raise error_factory()
        mock_event = MagicMock()
        mock_event.is_final_response.return_value = True
        mock_event.content = MagicMock()
        mock_event.content.parts = [MagicMock(text=findings)]
        return runner_run_async_returning([mock_event])()

    return run_async_side_effect


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_post_review_comments_always_one_by_one(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """Runner always posts comments one-by-one; there is no batch post_review_comments call.

    Each finding results in exactly one post_review_comments([c]) call so that
    provider-specific fields like line_type are preserved and one failing comment
    does not prevent the others from being posted.
    """

    def configure_provider(provider):
        provider.post_review_comments = MagicMock()
        provider.post_pr_summary_comment = MagicMock()

    findings_json = (
        '[{"path":"foo.py","line":1,"severity":"suggestion","code":"x","message":"Fix."},'
        '{"path":"foo.py","line":2,"severity":"critical","code":"y","message":"Bug."}]'
    )
    to_post, provider = _exercise_error_path(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_llm_config,
        mock_get_context_window,
        findings_json,
        configure_provider,
    )

    # Both findings returned
    assert len(to_post) == 2
    # post_review_comments called once per finding (no batch call)
    assert provider.post_review_comments.call_count == 2
    # Each call posts exactly one comment
    for call in provider.post_review_comments.call_args_list:
        comments_arg = call[0][3]
        assert len(comments_arg) == 1, "each call should post exactly one comment"
    # post_review_comment (base class) must NOT be called — it would strip line_type.
    provider.post_review_comment.assert_not_called()


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_post_review_comment_skipped_not_fallback_to_pr_summary(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """When per-comment inline posting fails, the comment is skipped — no PR summary fallback.

    This mirrors the tool-based (file-by-file / multi-shot) behaviour: if a comment cannot
    be posted inline, a WARNING is logged and the comment is simply dropped.  The runner
    must NOT call post_pr_summary_comment as a fallback for failed inline comments.
    """

    def configure_provider(provider):
        # All post_review_comments calls fail.
        provider.post_review_comments.side_effect = RuntimeError("inline failure")
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

    # Finding is returned in the result list (was found by the agent).
    assert len(to_post) == 1
    # post_review_comments called once (one comment, one attempt — no batch)
    assert provider.post_review_comments.call_count == 1
    # post_review_comment (base class) must NOT be called — it would strip line_type.
    provider.post_review_comment.assert_not_called()
    # Crucially: no PR summary fallback for the failed inline comment.
    # (There may still be a "Viper has started a review" summary comment,
    # but NOT one for the failing inline comment.)
    for call_args in provider.post_pr_summary_comment.call_args_list:
        # post_pr_summary_comment(owner, repo, pr_number, body) — body is [3]
        pos_args = call_args[0] if call_args[0] else ()
        body = pos_args[3] if len(pos_args) >= 4 else call_args[1].get("body", "")
        assert "foo.py:2" not in str(body), (
            "PR summary fallback for inline comment failure must be removed; "
            f"found unexpected summary body: {body!r}"
        )


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_file_by_file_skips_file_on_rate_limit_error(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """File-by-file mode skips a file and continues when a RateLimitError is raised."""
    call_count = [0]
    findings = '[{"path":"b.py","line":1,"severity":"info","code":"ok","message":"Fine."}]'

    run_async_side_effect = _build_file_by_file_run_async_side_effect(
        call_count, lambda: RateLimitError("HTTP 429 Too Many Requests"), findings
    )

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
    findings = (
        '[{"path":"b.py","line":2,"severity":"suggestion","code":"s","message":"Improve."}]'
    )

    run_async_side_effect = _build_file_by_file_run_async_side_effect(
        call_count, lambda: RuntimeError("unexpected LLM error"), findings
    )

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


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_marker_comment_posted_for_omit_marker_providers(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """For providers with omit_fingerprint_marker_in_body=True (e.g. Bitbucket Server),
    a PR-level run-marker comment must be posted when inline posting fully fails so
    idempotency can short-circuit subsequent runs.

    Without this comment, the run_id is never stored anywhere (inline markers are
    suppressed) and _idempotency_key_seen_in_comments returns False on every run,
    causing infinite retries even when all inline comments fail with 409.
    """

    def configure_provider(provider):
        provider.capabilities.return_value = ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=False,
            omit_fingerprint_marker_in_body=True,
        )
        provider.post_review_comments = MagicMock(side_effect=RuntimeError("409 Conflict"))
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

    assert len(to_post) == 1
    # Inline posting failed entirely, so a PR-level marker comment must be posted.
    assert provider.post_pr_summary_comment.call_count >= 1
    # The last call must contain the code-review-agent run marker in its body.
    last_body = provider.post_pr_summary_comment.call_args_list[-1][0][3]
    assert "code-review-agent:" in last_body, (
        "run-marker comment body must contain the code-review-agent marker so "
        "_idempotency_key_seen_in_comments can find the run_id on the next run"
    )
    assert "run=" in last_body, "run-marker comment must contain run=<run_id>"


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_marker_comment_not_posted_for_standard_providers(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """For providers with omit_fingerprint_marker_in_body=False (default, e.g. Gitea/GitHub),
    no extra PR-level run-marker comment is posted — the marker is embedded in inline
    comment bodies instead.
    """

    def configure_provider(provider):
        # omit_fingerprint_marker_in_body defaults to False
        provider.capabilities.return_value = ProviderCapabilities(
            resolvable_comments=False, supports_suggestions=False
        )
        provider.post_review_comments = MagicMock()
        provider.post_pr_summary_comment = MagicMock()

    findings_json = (
        '[{"path":"foo.py","line":1,"severity":"suggestion","code":"x","message":"Fix."}]'
    )
    _to_post, provider = _exercise_error_path(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_llm_config,
        mock_get_context_window,
        findings_json,
        configure_provider,
    )

    # No extra marker comment: markers are embedded in inline comment bodies.
    # (There may be a "started review" comment if PR description is empty, but
    # post_pr_summary_comment should not be called with a run-marker body.)
    for call_args in provider.post_pr_summary_comment.call_args_list:
        body = call_args[0][3] if call_args[0] else call_args[1].get("body", "")
        assert "run=" not in str(body) or "code-review-agent:" not in str(body), (
            "No run-marker PR comment expected for providers that embed markers in inline bodies"
        )


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_marker_comment_not_posted_when_inline_succeeds_for_omit_marker_providers(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """For omit-marker providers, successful inline posts must not add a visible run-marker comment."""

    def configure_provider(provider):
        provider.capabilities.return_value = ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=False,
            omit_fingerprint_marker_in_body=True,
        )
        provider.post_review_comments = MagicMock()
        provider.post_pr_summary_comment = MagicMock()

    findings_json = (
        '[{"path":"foo.py","line":1,"severity":"suggestion","code":"x","message":"Fix."}]'
    )
    _to_post, provider = _exercise_error_path(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_llm_config,
        mock_get_context_window,
        findings_json,
        configure_provider,
    )

    # Inline post succeeded, so no separate marker comment should be added.
    for call_args in provider.post_pr_summary_comment.call_args_list:
        body = call_args[0][3] if call_args[0] else call_args[1].get("body", "")
        assert "run=" not in str(body) or "code-review-agent:" not in str(body), (
            "No visible run-marker comment expected when inline posting succeeded"
        )
