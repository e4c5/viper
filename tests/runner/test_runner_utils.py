"""Focused tests for runner_utils collection helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from code_review.orchestration.runner_utils import (
    PartialResponseCollectionError,
    _collect_final_response_texts_async,
)
from code_review.providers.base import RateLimitError


def _final_event(author: str, text: str) -> MagicMock:
    event = MagicMock()
    event.author = author
    event.is_final_response.return_value = True
    event.content = MagicMock()
    event.content.parts = [MagicMock(text=text)]
    return event


def _run_async_with(*events: MagicMock, exc: Exception | None = None):
    async def _run_async(**_kwargs):
        for event in events:
            yield event
        if exc is not None:
            raise exc

    return _run_async


@pytest.mark.asyncio
async def test_collect_final_response_texts_async_wraps_partial_rate_limit_error():
    runner = SimpleNamespace(agent=MagicMock())
    runner.run_async = _run_async_with(
        _final_event("batch_review_0", '{"findings":[]}'),
        exc=RateLimitError("HTTP 429 Too Many Requests"),
    )

    with pytest.raises(PartialResponseCollectionError) as exc_info:
        await _collect_final_response_texts_async(runner, "session-1", MagicMock())

    assert exc_info.value.responses == [("batch_review_0", '{"findings":[]}')]
    assert isinstance(exc_info.value.cause, RateLimitError)


@pytest.mark.asyncio
async def test_collect_final_response_texts_async_does_not_wrap_runtime_error_before_any_events():
    runner = SimpleNamespace(agent=MagicMock())
    runner.run_async = _run_async_with(exc=RuntimeError("unexpected LLM error"))

    with pytest.raises(RuntimeError, match="unexpected LLM error"):
        await _collect_final_response_texts_async(runner, "session-1", MagicMock())


@pytest.mark.asyncio
async def test_collect_final_response_texts_async_wraps_post_event_runtime_error():
    runner = SimpleNamespace(agent=MagicMock())
    runner.run_async = _run_async_with(
        _final_event("batch_review_0", '{"findings":[]}'),
        exc=RuntimeError("unexpected LLM error"),
    )

    with pytest.raises(PartialResponseCollectionError) as exc_info:
        await _collect_final_response_texts_async(runner, "session-1", MagicMock())

    assert exc_info.value.responses == [("batch_review_0", '{"findings":[]}')]
    assert isinstance(exc_info.value.cause, RuntimeError)
