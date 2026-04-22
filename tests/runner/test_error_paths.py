"""Error-path tests for runner posting and provider failures (Phase 3.2)."""

from unittest.mock import MagicMock, patch

import pytest
from litellm import AuthenticationError

from code_review.diff.fingerprint import parse_marker_from_comment_body
from code_review.providers.base import FileInfo, ProviderCapabilities, RateLimitError
from tests.conftest import runner_run_async_returning


def _pr_summary_body_has_run_marker(body: str) -> bool:
    """True if body parses a code-review-agent marker with a run id (HTML or linkref)."""
    return parse_marker_from_comment_body(str(body)).get("run") is not None


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
    provider.get_pr_diff.return_value = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,1 +1,3 @@\n"
        "-line1\n"
        "+line1\n"
        "+line2\n"
        "+line3\n"
    )
    provider.get_file_content.return_value = "content"
    provider.get_existing_review_comments.return_value = []

    configure_provider(provider)
    mock_get_provider.return_value = provider

    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.author = "batch_review_0"
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=findings_json)]
    mock_runner_instance = MagicMock()
    mock_runner_instance.run_async = runner_run_async_returning([mock_event])

    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        to_post = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    return to_post, provider


def _exercise_batch_mode_failure(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_llm_config,
    mock_get_context_window,
    run_async_side_effect,
    *,
    dry_run: bool = False,
):
    """Helper: run batch mode with a custom run_async side effect."""
    from code_review.runner import run_review

    mock_get_scm_config.return_value = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="",
        skip_title_pattern="",
    )
    mock_get_llm_config.return_value = MagicMock(provider="gemini", model="gemini-3.1")
    mock_get_context_window.return_value = 1_000_000

    provider = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        resolvable_comments=False, supports_suggestions=False
    )
    provider.get_pr_files.return_value = [
        FileInfo(path="a.py", status="modified"),
        FileInfo(path="b.py", status="modified"),
    ]
    provider.get_pr_diff.return_value = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,1 +1,2 @@\n"
        "-old_a\n"
        "+new_a\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -1,1 +1,2 @@\n"
        "-old_b\n"
        "+new_b\n"
    )
    provider.get_file_content.return_value = ""
    provider.get_existing_review_comments.return_value = []
    provider.post_review_comments = MagicMock()
    provider.post_pr_summary_comment = MagicMock()
    mock_get_provider.return_value = provider

    mock_runner_instance = MagicMock()
    mock_runner_instance.run_async = run_async_side_effect

    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        results = run_review("o", "r", 1, head_sha="abc123", dry_run=dry_run)

    return results


def _final_batch_event(author: str, findings_json: str) -> MagicMock:
    event = MagicMock()
    event.is_final_response.return_value = True
    event.author = author
    event.content = MagicMock()
    event.content.parts = [MagicMock(text=findings_json)]
    return event


def _create_test_batch(batch_index: int = 0, paths: tuple[str, ...] = ("a.py", "b.py")):
    """Build a deterministic ReviewBatch fixture for batch-mode tests."""
    from code_review.batching import ReviewBatch, ReviewSegment

    segments = tuple(
        ReviewSegment(
            path=path,
            diff_text=f"diff --git a/{path} b/{path}",
            estimated_tokens=10,
            segment_index=0,
            total_segments=1,
            split_strategy="whole_file",
        )
        for path in paths
    )
    return ReviewBatch(
        batch_index=batch_index,
        estimated_tokens=len(paths) * 10,
        segments=segments,
        paths=paths,
    )


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
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
        '{"findings":['
        '{"path":"foo.py","line":1,"severity":"medium","code":"x","message":"Fix."},'
        '{"path":"foo.py","line":2,"severity":"high","code":"y","message":"Bug."}'
        ']}'
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


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_post_review_comment_skipped_not_fallback_to_pr_summary(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """When per-comment inline posting fails, the comment is skipped — no PR summary fallback.

    This mirrors the current tool-based inline-posting behaviour: if a comment cannot
    be posted inline, a WARNING is logged and the comment is simply dropped.  The runner
    must NOT call post_pr_summary_comment as a fallback for failed inline comments.
    """

    def configure_provider(provider):
        # All post_review_comments calls fail.
        provider.post_review_comments.side_effect = RuntimeError("inline failure")
        provider.post_pr_summary_comment = MagicMock()

    findings_json = (
        '{"findings":[{"path":"foo.py","line":2,"severity":"high","code":"x",'
        '"message":"Fix now."}]}'
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


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_batch_mode_rate_limit_error_is_fatal(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """Rate-limited batches are skipped while earlier successful batch responses are preserved."""

    calls = {"count": 0}

    def run_async_side_effect(*, new_message, **kwargs):
        del new_message, kwargs
        calls["count"] += 1
        current_call = calls["count"]

        async def _agen():
            if current_call == 1:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1,"severity":"medium","code":"x",'
                    '"message":"Fix a."}]}',
                )
                raise RateLimitError("HTTP 429 Too Many Requests")
            raise RateLimitError("HTTP 429 Too Many Requests")

        return _agen()

    results = _exercise_batch_mode_failure(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_llm_config,
        mock_get_context_window,
        run_async_side_effect,
    )

    assert [(finding.path, finding.message) for finding in results] == [("a.py", "Fix a.")]


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_batch_mode_propagates_rate_limit_error_for_whole_run(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """A workflow-level 429 falls back to isolated batches and skips only the rate-limited ones."""
    calls = {"count": 0}

    def run_async_side_effect(*, new_message, **kwargs):
        del new_message, kwargs
        calls["count"] += 1
        current_call = calls["count"]

        async def _agen():
            if current_call == 1:
                raise RateLimitError("HTTP 429 Too Many Requests")
            if current_call == 2:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1,"severity":"medium","code":"x",'
                    '"message":"Fix a."}]}',
                )
                return
            raise RateLimitError("HTTP 429 Too Many Requests")

        return _agen()

    findings = _exercise_batch_mode_failure(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_llm_config,
        mock_get_context_window,
        run_async_side_effect,
        dry_run=True,
    )

    assert [(finding.path, finding.message) for finding in findings] == [("a.py", "Fix a.")]


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_batch_mode_generic_error_is_fatal(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """Batch mode currently treats unexpected runner errors as fatal."""

    def run_async_side_effect(*, new_message, **kwargs):
        del new_message, kwargs
        raise RuntimeError("unexpected LLM error")

    with pytest.raises(RuntimeError, match="unexpected LLM error"):
        _exercise_batch_mode_failure(
            mock_get_scm_config,
            mock_get_provider,
            mock_get_llm_config,
            mock_get_context_window,
            run_async_side_effect,
        )


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_batch_mode_retries_malformed_completed_batches_before_rate_limit(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """A malformed completed batch must be retried even if a later batch hits a rate limit."""
    calls = {"count": 0}
    forced_batches = [
        _create_test_batch(batch_index=0, paths=("a.py",)),
        _create_test_batch(batch_index=1, paths=("b.py",)),
    ]

    def run_async_side_effect(*, new_message, **kwargs):
        del new_message, kwargs
        calls["count"] += 1
        current_call = calls["count"]

        async def _agen():
            if current_call == 1:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1},{"not":"valid"}]}',
                )
                raise RateLimitError("HTTP 429 Too Many Requests")
            if current_call == 2:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1,"severity":"medium","code":"x",'
                    '"message":"Retry malformed batch."}]}',
                )
                return
            if current_call == 3:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"b.py","line":1,"severity":"medium","code":"y",'
                    '"message":"Review remaining batch."}]}',
                )
                return
            raise AssertionError(f"unexpected run_async call #{calls['count']}")

        return _agen()

    with patch(
        "code_review.orchestration.standard_review.execution_mod.build_review_batches_for_scope",
        return_value=forced_batches,
    ):
        findings = _exercise_batch_mode_failure(
            mock_get_scm_config,
            mock_get_provider,
            mock_get_llm_config,
            mock_get_context_window,
            run_async_side_effect,
            dry_run=True,
        )

    assert [(finding.path, finding.message) for finding in findings] == [
        ("a.py", "Retry malformed batch."),
        ("b.py", "Review remaining batch."),
    ]


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_batch_mode_splits_malformed_batch_into_smaller_batches(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """Malformed batch recovery should shrink scope before retrying the same payload again."""
    calls = {"count": 0}
    forced_batches = [_create_test_batch()]

    def run_async_side_effect(*, new_message, **kwargs):
        del new_message, kwargs
        calls["count"] += 1
        current_call = calls["count"]

        async def _agen():
            if current_call == 1:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1},{"not":"valid"}]}',
                )
                return
            if current_call == 2:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1},{"not":"valid"}]}',
                )
                return
            if current_call == 3:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1,"severity":"medium","code":"x",'
                    '"message":"Recovered split batch A."}]}',
                )
                return
            if current_call == 4:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"b.py","line":1,"severity":"medium","code":"y",'
                    '"message":"Recovered split batch B."}]}',
                )
                return
            raise AssertionError(f"unexpected run_async call #{calls['count']}")

        return _agen()

    with patch(
        "code_review.orchestration.standard_review.execution_mod.build_review_batches_for_scope",
        return_value=forced_batches,
    ):
        findings = _exercise_batch_mode_failure(
            mock_get_scm_config,
            mock_get_provider,
            mock_get_llm_config,
            mock_get_context_window,
            run_async_side_effect,
            dry_run=True,
        )

    assert [(finding.path, finding.message) for finding in findings] == [
        ("a.py", "Recovered split batch A."),
        ("b.py", "Recovered split batch B."),
    ]


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_batch_mode_retries_empty_isolated_batch_response(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """An empty isolated-batch response must retry instead of being treated as success."""
    calls = {"count": 0}
    forced_batches = [_create_test_batch()]

    def run_async_side_effect(*, new_message, **kwargs):
        del new_message, kwargs
        calls["count"] += 1
        current_call = calls["count"]

        async def _agen():
            if current_call == 1:
                raise RateLimitError("HTTP 429 Too Many Requests")
            if current_call == 2:
                return
            if current_call == 3:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1,"severity":"medium","code":"x",'
                    '"message":"Recovered split batch A after empty response."}]}',
                )
                return
            if current_call == 4:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"b.py","line":1,"severity":"medium","code":"y",'
                    '"message":"Recovered split batch B after empty response."}]}',
                )
                return
            raise AssertionError(f"unexpected run_async call #{calls['count']}")

        return _agen()

    with patch(
        "code_review.orchestration.standard_review.execution_mod.build_review_batches_for_scope",
        return_value=forced_batches,
    ):
        findings = _exercise_batch_mode_failure(
            mock_get_scm_config,
            mock_get_provider,
            mock_get_llm_config,
            mock_get_context_window,
            run_async_side_effect,
            dry_run=True,
        )

    assert [(finding.path, finding.message) for finding in findings] == [
        ("a.py", "Recovered split batch A after empty response."),
        ("b.py", "Recovered split batch B after empty response."),
    ]


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_batch_mode_retries_missing_initial_sequential_response(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """A missing initial batch response must not be accepted as zero findings."""
    calls = {"count": 0}
    forced_batches = [_create_test_batch(paths=("a.py",))]

    def run_async_side_effect(*, new_message, **kwargs):
        del new_message, kwargs
        calls["count"] += 1
        current_call = calls["count"]

        async def _agen():
            if current_call == 1:
                return
            if current_call == 2:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1,"severity":"medium","code":"x",'
                    '"message":"Recovered after missing initial response."}]}',
                )
                return
            raise AssertionError(f"unexpected run_async call #{calls['count']}")

        return _agen()

    with patch(
        "code_review.orchestration.standard_review.execution_mod.build_review_batches_for_scope",
        return_value=forced_batches,
    ):
        findings = _exercise_batch_mode_failure(
            mock_get_scm_config,
            mock_get_provider,
            mock_get_llm_config,
            mock_get_context_window,
            run_async_side_effect,
            dry_run=True,
        )

    assert [(finding.path, finding.message) for finding in findings] == [
        ("a.py", "Recovered after missing initial response.")
    ]


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_batch_mode_split_batches_preserve_retry_attempt(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """Split malformed batches should inherit the current retry attempt."""
    calls = {"count": 0}
    prompt_texts: list[str] = []
    forced_batches = [_create_test_batch()]

    def run_async_side_effect(*, new_message, **kwargs):
        del kwargs
        prompt_texts.append(new_message.parts[0].text)
        calls["count"] += 1
        current_call = calls["count"]

        async def _agen():
            if current_call == 1:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1},{"not":"valid"}]}',
                )
                return
            if current_call == 2:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1},{"not":"valid"}]}',
                )
                return
            if current_call == 3:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"b.py","line":1,"severity":"medium","code":"y",'
                    '"message":"Recovered split batch B after retry."}]}',
                )
                return
            if current_call == 4:
                yield _final_batch_event(
                    "batch_review_0",
                    '{"findings":[{"path":"a.py","line":1,"severity":"medium","code":"x",'
                    '"message":"Recovered split batch A after retry."}]}',
                )
                return
            raise AssertionError(f"unexpected run_async call #{current_call}")

        return _agen()

    with patch(
        "code_review.orchestration.standard_review.execution_mod.build_review_batches_for_scope",
        return_value=forced_batches,
    ):
        findings = _exercise_batch_mode_failure(
            mock_get_scm_config,
            mock_get_provider,
            mock_get_llm_config,
            mock_get_context_window,
            run_async_side_effect,
            dry_run=True,
        )

    assert [(finding.path, finding.message) for finding in findings] == [
        ("b.py", "Recovered split batch B after retry."),
        ("a.py", "Recovered split batch A after retry."),
    ]
    retry_note = "Your previous response was interrupted and resulted in invalid, truncated JSON."
    assert retry_note not in prompt_texts[0]
    assert retry_note in prompt_texts[1]
    assert retry_note in prompt_texts[2]
    assert retry_note in prompt_texts[3]


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_batch_mode_authentication_error_is_fatal(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """
    Batch mode must treat litellm.AuthenticationError (HTTP 401) as fatal.
    """
    def make_auth_error():
        # AuthenticationError(message, llm_provider, model, response=None)
        return AuthenticationError(
            "HTTP 401 Unauthorized", llm_provider="openrouter", model="openrouter/gpt-4o"
        )

    def run_async_side_effect(*, new_message, **kwargs):
        del new_message, kwargs
        raise make_auth_error()

    with pytest.raises(AuthenticationError):
        _exercise_batch_mode_failure(
            mock_get_scm_config,
            mock_get_provider,
            mock_get_llm_config,
            mock_get_context_window,
            run_async_side_effect,
        )


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_run_marker_comment_posted_for_omit_marker_providers(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """When all inline posts fail, omit-marker providers still get a visible PR summary but
    no run= idempotency marker so the next CI run can retry posting (not short-circuit).
    """

    def configure_provider(provider):
        provider.capabilities.return_value = ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=False,
            omit_fingerprint_marker_in_body=True,
            embed_agent_marker_as_commonmark_linkref=True,
        )
        provider.post_review_comments = MagicMock(side_effect=RuntimeError("409 Conflict"))
        provider.post_pr_summary_comment = MagicMock()
        provider.get_pr_info.return_value = MagicMock(description="x" * 50, title="title")

    findings_json = (
        '{"findings":[{"path":"foo.py","line":1,"severity":"medium","code":"x",'
        '"message":"Fix."}]}'
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
    assert provider.post_pr_summary_comment.call_count >= 1
    bodies = [
        (c[0][3] if c[0] else c[1].get("body", ""))
        for c in provider.post_pr_summary_comment.call_args_list
    ]
    marker_bodies = [b for b in bodies if _pr_summary_body_has_run_marker(b)]
    assert not marker_bodies, "no run idempotency marker when inline posts all failed"
    assert any("Viper" in str(b) for b in bodies)
    assert any("inline comment" in str(b).lower() for b in bodies)


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
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
        '{"findings":[{"path":"foo.py","line":1,"severity":"medium","code":"x",'
        '"message":"Fix."}]}'
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


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_run_marker_pr_summary_posted_when_inline_succeeds_for_omit_marker_providers(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """For omit-marker providers, a PR-level Viper summary with run marker is posted
    even when inline comments succeed, so reruns with the same head/config skip the agent."""

    def configure_provider(provider):
        provider.capabilities.return_value = ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=False,
            omit_fingerprint_marker_in_body=True,
            embed_agent_marker_as_commonmark_linkref=True,
        )
        provider.post_review_comments = MagicMock()
        provider.post_pr_summary_comment = MagicMock()
        provider.get_pr_info.return_value = MagicMock(description="x" * 50, title="title")

    findings_json = (
        '{"findings":[{"path":"foo.py","line":1,"severity":"medium","code":"x",'
        '"message":"Fix."}]}'
    )
    _to_post, provider = _exercise_error_path(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_llm_config,
        mock_get_context_window,
        findings_json,
        configure_provider,
    )

    bodies = [
        (c[0][3] if c[0] else c[1].get("body", ""))
        for c in provider.post_pr_summary_comment.call_args_list
    ]
    marker_bodies = [b for b in bodies if _pr_summary_body_has_run_marker(b)]
    assert marker_bodies, "expected PR summary with idempotency marker after successful inline post"
    assert any("Viper" in str(b) for b in marker_bodies)
    assert any("posted" in str(b).lower() for b in marker_bodies)
