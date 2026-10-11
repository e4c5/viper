"""Tests for retrying batches whose reasoning consumed the whole output allowance.

Production failure (e4c5/indrani#297, deepseek-flash): a batch kept stopping on
finish_reason=MAX_TOKENS with thoughts_tokens == completion_tokens, i.e. the model
never emitted any JSON. Splitting the batch did not help, so 20 files went unreviewed.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_review import batching
from code_review.config import LLMConfig
from code_review.models import PRContext
from code_review.orchestration import execution as execution_mod
from code_review.orchestration import runner_utils


def _segment(path):
    diff_text = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,2 @@\n"
        "-old\n"
        "+new\n"
    )
    return batching.ReviewSegment(
        path=path,
        diff_text=diff_text,
        estimated_tokens=10,
        segment_index=0,
        total_segments=1,
        split_strategy="whole_file",
    )


def _make_batch(*paths, index=0):
    segments = tuple(_segment(p) for p in paths)
    return batching.ReviewBatch(
        batch_index=index,
        estimated_tokens=10 * len(segments),
        segments=segments,
        paths=tuple(paths),
    )


def _cfg(provider="deepseek", **kwargs):
    return LLMConfig(
        provider=provider,
        model="deepseek-flash" if provider == "deepseek" else "gpt-4.1-mini",
        max_retries=0,
        timeout_seconds=0,
        **kwargs,
    )


@pytest.fixture
def plumbing():
    """Patch per-batch agent plumbing; expose the create_agent_and_runner mock."""
    with (
        patch.object(
            execution_mod,
            "create_agent_and_runner",
            return_value=("session-id", None, object()),
        ) as create,
        patch.object(execution_mod, "_attach_batch_user_messages", return_value=None),
        patch.object(execution_mod, "build_batch_review_content", return_value=object()),
        patch.object(execution_mod, "_sleep"),
    ):
        yield create


def _efforts(create_mock):
    """reasoning_effort of the llm_config passed to each create_agent_and_runner call."""
    return [
        getattr(call.kwargs.get("llm_config"), "reasoning_effort", None)
        for call in create_mock.call_args_list
    ]


def _truncated_then(*results):
    """Build a collect side effect: each result is (text, truncated)."""
    queue = list(results)

    def collect(*_args, truncated_authors=None, **_kwargs):
        text, truncated = queue.pop(0)
        if truncated and truncated_authors is not None:
            truncated_authors.add("batch_review_0")
        return [("batch_review_0", text)]

    return collect


# ---------------------------------------------------------------------------
# Response collection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (SimpleNamespace(name="MAX_TOKENS"), True),
        ("FinishReason.MAX_TOKENS", True),
        (SimpleNamespace(name="STOP"), False),
        (None, False),
    ],
)
def test_is_max_tokens_finish(reason, expected):
    assert runner_utils.is_max_tokens_finish(SimpleNamespace(finish_reason=reason)) is expected


class _TruncatingRunner:
    agent = SimpleNamespace(sub_agents=[])

    def run_async(self, **_kwargs):
        return self._gen()

    async def _gen(self):
        yield SimpleNamespace(
            author="batch_review_1",
            content=SimpleNamespace(parts=[SimpleNamespace(text="{", thought=False)]),
            is_final_response=lambda: True,
            finish_reason=SimpleNamespace(name="MAX_TOKENS"),
        )
        await asyncio.sleep(0)


def test_collector_records_truncated_authors():
    truncated: set[str] = set()
    responses = runner_utils._run_agent_and_collect_responses(
        _TruncatingRunner(), "session-1", SimpleNamespace(), truncated_authors=truncated
    )
    assert responses == [("batch_review_1", "{")]
    assert truncated == {"batch_review_1"}


# ---------------------------------------------------------------------------
# Isolated retry
# ---------------------------------------------------------------------------


def test_truncated_batch_is_retried_with_reasoning_disabled(plumbing):
    collect = _truncated_then(("{\"findings\": [", True), ('{"findings":[]}', False))
    batch = _make_batch("a.py", "b.py")
    with patch.object(
        execution_mod.runner_mod, "_run_agent_and_collect_responses", side_effect=collect
    ):
        findings, unreviewed = execution_mod._run_isolated_batches_with_retry(
            pr_ctx=object(),
            provider=object(),
            review_standards="",
            batches_to_run=[batch],
            context_brief_attached=False,
            prompt_suffix="",
            llm_config=_cfg(),
            initial_retry_attempt=1,
        )
    assert unreviewed == []
    # Same, unsplit batch re-run once with thinking off; no attempt consumed.
    assert _efforts(plumbing) == [None, "none"]
    assert [c.args[3] for c in plumbing.call_args_list] == [[batch], [batch]]


def test_initially_truncated_batch_starts_with_reasoning_disabled(plumbing):
    collect = _truncated_then(('{"findings":[]}', False))
    batch = _make_batch("a.py")
    with patch.object(
        execution_mod.runner_mod, "_run_agent_and_collect_responses", side_effect=collect
    ):
        _findings, unreviewed = execution_mod._run_isolated_batches_with_retry(
            pr_ctx=object(),
            provider=object(),
            review_standards="",
            batches_to_run=[batch],
            context_brief_attached=False,
            prompt_suffix="",
            llm_config=_cfg(),
            initial_retry_attempt=1,
            truncated_batches=[batch],
        )
    assert unreviewed == []
    assert _efforts(plumbing) == ["none"]


def test_still_truncated_with_reasoning_off_falls_back_to_split_and_terminates(plumbing):
    collect = MagicMock(side_effect=_truncated_then(*[("{", True)] * 20))
    batch = _make_batch("a.py", "b.py")
    with patch.object(
        execution_mod.runner_mod, "_run_agent_and_collect_responses", collect
    ):
        _findings, unreviewed = execution_mod._run_isolated_batches_with_retry(
            pr_ctx=object(),
            provider=object(),
            review_standards="",
            batches_to_run=[batch],
            context_brief_attached=False,
            prompt_suffix="",
            llm_config=_cfg(),
            initial_retry_attempt=1,
        )
    assert {p for b in unreviewed for p in b.paths} == {"a.py", "b.py"}
    efforts = _efforts(plumbing)
    # Reasoning is switched off once, and the split children inherit it.
    assert efforts[0] is None
    assert all(e == "none" for e in efforts[1:])
    assert collect.call_count < 10


def test_non_toggle_provider_keeps_existing_split_behaviour(plumbing):
    collect = _truncated_then(("{", True), ('{"findings":[]}', False), ('{"findings":[]}', False))
    with patch.object(
        execution_mod.runner_mod, "_run_agent_and_collect_responses", side_effect=collect
    ):
        _findings, unreviewed = execution_mod._run_isolated_batches_with_retry(
            pr_ctx=object(),
            provider=object(),
            review_standards="",
            batches_to_run=[_make_batch("a.py", "b.py")],
            context_brief_attached=False,
            prompt_suffix="",
            llm_config=_cfg(provider="openai"),
            initial_retry_attempt=1,
        )
    assert unreviewed == []
    assert _efforts(plumbing) == [None, None, None]


def test_explicit_none_effort_is_not_retoggled(plumbing):
    collect = _truncated_then(("{", True), ('{"findings":[]}', False), ('{"findings":[]}', False))
    with patch.object(
        execution_mod.runner_mod, "_run_agent_and_collect_responses", side_effect=collect
    ):
        execution_mod._run_isolated_batches_with_retry(
            pr_ctx=object(),
            provider=object(),
            review_standards="",
            batches_to_run=[_make_batch("a.py", "b.py")],
            context_brief_attached=False,
            prompt_suffix="",
            llm_config=_cfg(reasoning_effort="none"),
            initial_retry_attempt=1,
        )
    # Already off: goes straight to splitting (3 calls: original + two halves).
    assert len(plumbing.call_args_list) == 3


# ---------------------------------------------------------------------------
# Sequential workflow → isolated retry hand-off
# ---------------------------------------------------------------------------


def test_sequential_truncation_hands_batch_to_reasoning_off_retry(plumbing):
    calls = {"n": 0}

    def collect(*_args, truncated_authors=None, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            truncated_authors.add("batch_review_0")
            return [("batch_review_0", "{"), ("batch_review_1", '{"findings":[]}')]
        return [("batch_review_0", '{"findings":[]}')]

    batches = [_make_batch("a.py", index=0), _make_batch("b.py", index=1)]
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
            llm_config=_cfg(),
        )
    assert outcome.coverage_complete
    assert calls["n"] == 2
    # The single isolated retry ran batch 0 with thinking already off.
    assert _efforts(plumbing) == ["none"]
    assert plumbing.call_args_list[0].args[3] == [batches[0]]
