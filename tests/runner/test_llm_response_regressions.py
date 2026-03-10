"""Targeted reproductions for LLM response-stream regressions in runner."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from google.genai import types

from tests.conftest import runner_run_async_returning
from code_review.providers.base import FileInfo
from code_review.runner import _collect_response_async, _findings_from_response, run_review


class _SessionServiceStub:
    async def create_session(self, **kwargs):  # pragma: no cover - trivial stub
        return None


def _event(is_final: bool, parts: list[MagicMock]) -> MagicMock:
    evt = MagicMock()
    evt.is_final_response.return_value = is_final
    evt.content = MagicMock()
    evt.content.parts = parts
    return evt


def test_collect_response_uses_non_final_json_when_final_has_non_text_only():
    """When final event has no text, collector falls back to non-final text JSON."""
    non_final_json = (
        '[{"path":"foo.py","line":1,"severity":"info","code":"x","message":"issue"}]'
    )
    events = [
        _event(False, [MagicMock(text=non_final_json)]),
        _event(True, [MagicMock(text=None, function_call={"name": "noop"})]),
    ]
    runner = MagicMock()
    runner.run_async = runner_run_async_returning(events)
    content = types.Content(role="user", parts=[types.Part(text="review")])

    text = asyncio.run(
        _collect_response_async(runner, _SessionServiceStub(), "session-non-final-json", content)
    )

    assert text == non_final_json
    findings = _findings_from_response(text)
    assert len(findings) == 1
    assert findings[0].path == "foo.py"


def test_collect_response_drops_when_final_has_only_non_text_parts():
    """Repro: final event without text yields empty collected response."""
    events = [_event(True, [MagicMock(text=None, function_call={"name": "tool_call"})])]
    runner = MagicMock()
    runner.run_async = runner_run_async_returning(events)
    content = types.Content(role="user", parts=[types.Part(text="review")])

    text = asyncio.run(
        _collect_response_async(runner, _SessionServiceStub(), "session-non-text-final", content)
    )

    assert text == ""


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_retries_run_async_rate_limit_and_recovers(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """Transient run_async rate-limit errors are retried using llm config."""
    mock_get_scm_config.return_value = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="",
        skip_title_pattern="",
    )
    mock_get_llm_config.return_value = MagicMock(
        provider="gemini",
        model="gemini-2.5-flash",
        max_retries=2,
        timeout_seconds=60.0,
    )
    mock_get_context_window.return_value = 1_000_000

    provider = MagicMock()
    provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    provider.get_pr_diff.return_value = "diff --git a/foo.py b/foo.py"
    provider.get_file_content.return_value = "content"
    provider.get_existing_review_comments.return_value = []
    provider.get_pr_info.return_value = None
    mock_get_provider.return_value = provider

    call_count = {"count": 0}

    findings_json = (
        '[{"path":"foo.py","line":2,"severity":"suggestion","code":"x","message":"fix"}]'
    )

    def _run_async(**kwargs):
        call_count["count"] += 1
        if call_count["count"] == 1:
            async def _rate_limited_gen():
                raise RuntimeError("429 Too Many Requests")
                yield  # pragma: no cover
            return _rate_limited_gen()
        event = _event(True, [MagicMock(text=findings_json)])
        return runner_run_async_returning([event])()

    mock_runner = MagicMock()
    mock_runner.run_async = _run_async

    with patch("google.adk.runners.Runner", return_value=mock_runner):
        result = run_review("o", "r", 1, head_sha="abc123", dry_run=True)

    assert call_count["count"] == 2
    assert len(result) == 1
    assert result[0].line == 2


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_raises_after_retry_budget_exhausted(
    mock_get_scm_config, mock_get_provider, mock_get_llm_config, mock_get_context_window
):
    """run_async errors still bubble after configured retries are exhausted."""
    mock_get_scm_config.return_value = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="",
        skip_title_pattern="",
    )
    mock_get_llm_config.return_value = MagicMock(
        provider="gemini",
        model="gemini-2.5-flash",
        max_retries=1,
        timeout_seconds=60.0,
    )
    mock_get_context_window.return_value = 1_000_000

    provider = MagicMock()
    provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    provider.get_pr_diff.return_value = "diff --git a/foo.py b/foo.py"
    provider.get_file_content.return_value = "content"
    provider.get_existing_review_comments.return_value = []
    provider.get_pr_info.return_value = None
    mock_get_provider.return_value = provider

    call_count = {"count": 0}

    def _run_async(**kwargs):
        call_count["count"] += 1
        async def _rate_limited_gen():
            raise RuntimeError("429 Too Many Requests")
            yield  # pragma: no cover
        return _rate_limited_gen()

    mock_runner = MagicMock()
    mock_runner.run_async = _run_async

    with patch("google.adk.runners.Runner", return_value=mock_runner):
        with pytest.raises(RuntimeError, match="429 Too Many Requests"):
            run_review("o", "r", 1, head_sha="abc123", dry_run=True)

    assert call_count["count"] == 2
