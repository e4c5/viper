"""Focused tests for runner_utils collection helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_review.orchestration.runner_utils import (
    PartialResponseCollectionError,
    _collect_final_response_texts_async,
    _get_output_key_findings,
    _read_output_key_findings_async,
)
from code_review.providers.base import RateLimitError
from code_review.schemas.findings import FindingV1


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


# ---------------------------------------------------------------------------
# _read_output_key_findings_async / _get_output_key_findings
# ---------------------------------------------------------------------------

def _make_session(state: dict):
    session = MagicMock()
    session.state = state
    return session


def _make_session_service(session=None, raise_exc=None):
    svc = MagicMock()
    if raise_exc is not None:
        svc.get_session = AsyncMock(side_effect=raise_exc)
    else:
        svc.get_session = AsyncMock(return_value=session)
    return svc


@pytest.mark.asyncio
async def test_read_output_key_findings_returns_findings_from_dict():
    raw = {"findings": [{"path": "a.py", "line": 1, "severity": "medium", "code": "x", "message": "m"}]}
    svc = _make_session_service(_make_session({"findings_result": raw}))

    result = await _read_output_key_findings_async(svc, "sid", "findings_result")

    assert result is not None
    assert len(result) == 1
    assert isinstance(result[0], FindingV1)
    assert result[0].path == "a.py"


@pytest.mark.asyncio
async def test_read_output_key_findings_returns_none_when_session_missing():
    svc = _make_session_service(session=None)

    result = await _read_output_key_findings_async(svc, "sid", "findings_result")

    assert result is None


@pytest.mark.asyncio
async def test_read_output_key_findings_returns_none_when_key_absent():
    svc = _make_session_service(_make_session({}))

    result = await _read_output_key_findings_async(svc, "sid", "findings_result")

    assert result is None


@pytest.mark.asyncio
async def test_read_output_key_findings_returns_none_on_get_session_error():
    svc = _make_session_service(raise_exc=RuntimeError("DB down"))

    result = await _read_output_key_findings_async(svc, "sid", "findings_result")

    assert result is None


@pytest.mark.asyncio
async def test_read_output_key_findings_returns_none_on_invalid_schema():
    raw = {"findings": [{"bad": "schema"}]}
    svc = _make_session_service(_make_session({"findings_result": raw}))

    result = await _read_output_key_findings_async(svc, "sid", "findings_result")

    assert result is None


def test_get_output_key_findings_sync_wrapper():
    raw = {"findings": [{"path": "b.py", "line": 5, "severity": "high", "code": "sec", "message": "Issue"}]}
    svc = _make_session_service(_make_session({"findings_result": raw}))

    result = _get_output_key_findings(svc, "sid", "findings_result")

    assert result is not None
    assert len(result) == 1
    assert result[0].path == "b.py"
