"""Tests for transient LLM error recognition, idle timeout, and retry backoff (W2),
plus unreviewed-batch coverage accounting (W1)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_review import batching
from code_review.models import PRContext
from code_review.orchestration import execution as execution_mod
from code_review.orchestration_deps import (
    LLMTimeoutError,
    PartialResponseCollectionError,
    RateLimitError,
    is_transient_llm_error,
    retry_after_seconds,
)


def _make_batch(path="foo.py"):
    diff_text = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,1 +1,2 @@\n"
        "-old\n"
        "+new\n"
    )
    segment = batching.ReviewSegment(
        path=path,
        diff_text=diff_text,
        estimated_tokens=10,
        segment_index=0,
        total_segments=1,
        split_strategy="whole_file",
    )
    return batching.ReviewBatch(
        batch_index=0,
        estimated_tokens=10,
        segments=(segment,),
        paths=(path,),
    )


class _HTTPStatusError(Exception):
    def __init__(self, message, *, status_code=None, code=None, response=None, details=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        if response is not None:
            self.response = response
        if details is not None:
            self.details = details


# ---------------------------------------------------------------------------
# is_transient_llm_error
# ---------------------------------------------------------------------------


def test_is_transient_llm_error_recognizes_scm_rate_limit():
    assert is_transient_llm_error(RateLimitError("slow down")) is True


def test_is_transient_llm_error_recognizes_status_code_429():
    assert is_transient_llm_error(_HTTPStatusError("limited", status_code=429)) is True


def test_is_transient_llm_error_recognizes_genai_style_code_503():
    assert is_transient_llm_error(_HTTPStatusError("unavailable", code=503)) is True


def test_is_transient_llm_error_recognizes_llm_timeout():
    assert is_transient_llm_error(LLMTimeoutError("idle")) is True


def test_is_transient_llm_error_walks_cause_chain():
    try:
        try:
            raise _HTTPStatusError("limited", status_code=429)
        except _HTTPStatusError as inner:
            raise RuntimeError("batch failed") from inner
    except RuntimeError as wrapper:
        assert is_transient_llm_error(wrapper) is True


def test_is_transient_llm_error_rejects_plain_value_error():
    assert is_transient_llm_error(ValueError("bad json")) is False


# ---------------------------------------------------------------------------
# retry_after_seconds
# ---------------------------------------------------------------------------


def test_retry_after_seconds_reads_response_header():
    exc = _HTTPStatusError(
        "limited",
        status_code=429,
        response=SimpleNamespace(headers={"retry-after": "7"}),
    )
    assert retry_after_seconds(exc) == 7.0


def test_retry_after_seconds_reads_genai_details():
    exc = _HTTPStatusError(
        "limited",
        code=429,
        details=[{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}],
    )
    assert retry_after_seconds(exc) == 12.0


def test_retry_after_seconds_returns_none_when_absent():
    assert retry_after_seconds(ValueError("nope")) is None
    assert (
        retry_after_seconds(
            _HTTPStatusError("x", response=SimpleNamespace(headers={"content-type": "json"}))
        )
        is None
    )


def test_retry_after_seconds_walks_cause_chain():
    try:
        try:
            raise _HTTPStatusError(
                "limited",
                status_code=429,
                response=SimpleNamespace(headers={"Retry-After": "3"}),
            )
        except _HTTPStatusError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as wrapper:
        assert retry_after_seconds(wrapper) == 3.0


# ---------------------------------------------------------------------------
# Idle timeout
# ---------------------------------------------------------------------------


def _final_event(author: str, text: str):
    return SimpleNamespace(
        author=author,
        content=SimpleNamespace(parts=[SimpleNamespace(text=text, thought=False)]),
        is_final_response=lambda: True,
    )


class _IdleForeverRunner:
    """Fake runner: emits one event then hangs forever."""

    agent = SimpleNamespace(sub_agents=[])

    def run_async(self, **_kwargs):
        return self._gen()

    async def _gen(self):
        yield _final_event("batch_review_0", '{"findings":[]}')
        await asyncio.Event().wait()


def test_idle_timeout_raises_llm_timeout_and_preserves_partial_responses():
    from code_review.orchestration import runner_utils

    with pytest.raises(PartialResponseCollectionError) as excinfo:
        runner_utils._run_agent_and_collect_responses(
            _IdleForeverRunner(),
            "session-1",
            SimpleNamespace(),
            idle_timeout_seconds=0.05,
        )

    assert isinstance(excinfo.value.cause, LLMTimeoutError)
    assert excinfo.value.responses == [("batch_review_0", '{"findings":[]}')]


# ---------------------------------------------------------------------------
# Isolated-retry backoff
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_retry_patches():
    """Patch the per-batch agent plumbing so only the LLM call is exercised."""
    with (
        patch.object(
            execution_mod,
            "create_agent_and_runner",
            return_value=("session-id", None, object()),
        ),
        patch.object(execution_mod, "_attach_batch_user_messages", return_value=None),
        patch.object(execution_mod, "build_batch_review_content", return_value=object()),
        patch.object(execution_mod, "_sleep") as mock_sleep,
    ):
        yield mock_sleep


def _llm_cfg(max_retries=3):
    return SimpleNamespace(max_retries=max_retries, timeout_seconds=None)


def test_transient_retry_uses_retry_after_delay(isolated_retry_patches):
    mock_sleep = isolated_retry_patches
    transient = _HTTPStatusError(
        "limited",
        status_code=429,
        response=SimpleNamespace(headers={"retry-after": "5"}),
    )
    good_response = [("batch_review_0", '{"findings":[]}')]
    collect = MagicMock(
        side_effect=[
            PartialResponseCollectionError(responses=[], cause=transient),
            good_response,
        ]
    )
    with patch.object(
        execution_mod.runner_mod, "_run_agent_and_collect_responses", collect
    ):
        findings, unreviewed = execution_mod._run_isolated_batches_with_retry(
            pr_ctx=object(),
            provider=object(),
            review_standards="",
            batches_to_run=[_make_batch()],
            context_brief_attached=False,
            prompt_suffix="",
            llm_config=_llm_cfg(),
        )
    assert unreviewed == []
    mock_sleep.assert_called_once_with(5.0)


def test_transient_retry_exhaustion_marks_batch_unreviewed(isolated_retry_patches):
    mock_sleep = isolated_retry_patches
    transient = _HTTPStatusError("limited", status_code=503)
    collect = MagicMock(
        side_effect=PartialResponseCollectionError(responses=[], cause=transient)
    )
    with patch.object(
        execution_mod.runner_mod, "_run_agent_and_collect_responses", collect
    ):
        findings, unreviewed = execution_mod._run_isolated_batches_with_retry(
            pr_ctx=object(),
            provider=object(),
            review_standards="",
            batches_to_run=[_make_batch()],
            context_brief_attached=False,
            prompt_suffix="",
            llm_config=_llm_cfg(max_retries=2),
        )
    assert findings == []
    assert [b.paths for b in unreviewed] == [("foo.py",)]
    assert mock_sleep.call_count == 2


def test_sequential_mode_recovers_from_litellm_style_429(isolated_retry_patches):
    """A 429 surfacing mid-sequential-run now triggers isolated retry instead of raising."""
    transient = _HTTPStatusError("rate limited", status_code=429)
    calls = {"n": 0}

    def collect(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # First (sequential) call dies on a transient 429 with one batch done.
            raise PartialResponseCollectionError(
                responses=[("batch_review_0", '{"findings":[]}')],
                cause=transient,
            )
        return [("batch_review_0", '{"findings":[]}')]

    batches = [_make_batch("a.py"), _make_batch("b.py")]
    runner = MagicMock()
    with patch.object(
        execution_mod.runner_mod, "_run_agent_and_collect_responses", side_effect=collect
    ):
        outcome = execution_mod.run_agent_and_collect_findings(
            PRContext("o", "r", 1, "sha1"),
            MagicMock(),
            "standards",
            runner,
            "session-1",
            batches,
            llm_config=_llm_cfg(max_retries=1),
        )
    assert outcome.coverage_complete
    # b.py's batch was retried in isolation via create_agent_and_runner path.
    assert calls["n"] >= 2


def test_unrecognised_author_marks_all_batches_missing(isolated_retry_patches):
    """Responses only from an unrecognised author => every batch is retried."""
    calls = {"n": 0}

    def collect(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [("<unknown>", '{"findings":[]}')]
        return [("batch_review_0", '{"findings":[]}')]

    batches = [_make_batch("a.py"), _make_batch("b.py")]
    with patch.object(
        execution_mod.runner_mod, "_run_agent_and_collect_responses", side_effect=collect
    ):
        outcome = execution_mod.run_agent_and_collect_findings(
            PRContext("o", "r", 1, "sha1"),
            MagicMock(),
            "standards",
            MagicMock(),
            "session-1",
            batches,
            llm_config=_llm_cfg(),
        )
    # Both batches were re-run in isolation: sequential call + 2 isolated calls.
    assert calls["n"] == 3
    assert outcome.coverage_complete


# ---------------------------------------------------------------------------
# W1: post_findings_and_summary forces REQUEST_CHANGES on incomplete coverage
# ---------------------------------------------------------------------------


def _make_handler(dry_run=False):
    from code_review.orchestration.context_enricher import ContextEnricher
    from code_review.orchestration.reply_dismissal import ReplyDismissalHandler
    from code_review.orchestration.review_decision import ReviewDecisionHandler
    from code_review.orchestration.standard_review import StandardReviewHandler

    pr_ctx = PRContext("o", "r", 1, "abc123")
    reply_handler = ReplyDismissalHandler(
        pr_ctx, dry_run=dry_run, event_context=None, run_reply_dismissal_llm=lambda _: ""
    )
    decision_handler = ReviewDecisionHandler(
        pr_ctx,
        dry_run=dry_run,
        event_context=None,
        reply_dismissal_handler=reply_handler,
        result_builder=MagicMock(),
        skip_if_needed=MagicMock(),
    )
    return StandardReviewHandler(
        pr_ctx,
        dry_run=dry_run,
        print_findings=False,
        context_enricher=ContextEnricher(pr_ctx),
        review_decision_handler=decision_handler,
        result_builder=MagicMock(),
    )


def _gate_outcome(decision="APPROVE"):
    from code_review.quality.outcome import QualityGateOutcome

    return QualityGateOutcome(
        high_count=0, medium_count=0, decision=decision, submission_reason="ok"
    )


def test_post_findings_and_summary_forces_request_changes_on_incomplete_coverage():
    handler = _make_handler(dry_run=False)
    provider = MagicMock()
    provider.capabilities.return_value = SimpleNamespace(
        supports_review_decisions=True,
        omit_fingerprint_marker_in_body=False,
        resolvable_comments=False,
    )
    cfg = MagicMock()
    cfg.review_decision_enabled = True

    with patch(
        "code_review.orchestration.standard_review.QualityGate"
    ) as mock_gate_cls:
        mock_gate_cls.return_value.evaluate.return_value = _gate_outcome("APPROVE")
        handler.post_findings_and_summary(
            provider,
            "",
            [],
            cfg,
            MagicMock(),
            [],
            unreviewed_paths=("src/foo.py",),
        )

    provider.submit_review_decision.assert_called_once()
    args, kwargs = provider.submit_review_decision.call_args
    assert args[3] == "REQUEST_CHANGES"
    provider.post_pr_summary_comment.assert_called_once()
    body = provider.post_pr_summary_comment.call_args[0][3]
    assert "src/foo.py" in body
    assert "re-run" in body


def test_post_findings_and_summary_approves_with_complete_coverage():
    handler = _make_handler(dry_run=False)
    provider = MagicMock()
    provider.capabilities.return_value = SimpleNamespace(
        supports_review_decisions=True,
        omit_fingerprint_marker_in_body=False,
        resolvable_comments=False,
    )
    provider.is_bot_currently_approved.return_value = False
    cfg = MagicMock()
    cfg.review_decision_enabled = True

    with patch(
        "code_review.orchestration.standard_review.QualityGate"
    ) as mock_gate_cls:
        mock_gate_cls.return_value.evaluate.return_value = _gate_outcome("APPROVE")
        handler.post_findings_and_summary(
            provider,
            "",
            [],
            cfg,
            MagicMock(),
            [],
            unreviewed_paths=(),
        )

    provider.submit_review_decision.assert_called_once()
    args, _kwargs = provider.submit_review_decision.call_args
    assert args[3] == "APPROVE"
    provider.post_pr_summary_comment.assert_not_called()
