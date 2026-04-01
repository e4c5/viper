"""Tests for the minimal local evaluation harness."""

from unittest.mock import AsyncMock, MagicMock, patch

from code_review.evals import (
    run_local_golden_pr_review_eval,
    run_local_reply_dismissal_eval,
)
from tests.conftest import runner_run_async_returning


def test_run_local_golden_pr_review_eval_passes_checked_in_cases() -> None:
    summary = run_local_golden_pr_review_eval()

    assert summary.suite_name == "golden_pr_review[parser]"
    assert summary.total >= 2
    assert summary.failed == 0
    assert summary.passed == summary.total


def test_run_local_reply_dismissal_eval_passes_checked_in_cases() -> None:
    summary = run_local_reply_dismissal_eval()

    assert summary.suite_name == "reply_dismissal[parser]"
    assert summary.total >= 2
    assert summary.failed == 0
    assert summary.passed == summary.total


@patch("google.adk.sessions.InMemorySessionService")
@patch("google.adk.runners.Runner")
@patch("code_review.evals.local_runner.create_review_agent")
def test_run_local_golden_pr_review_eval_adk_mode(
    mock_create_review_agent, mock_runner_cls, mock_session_service_cls
) -> None:
    mock_create_review_agent.return_value = MagicMock(name="review_agent")
    session_service = MagicMock()
    session_service.create_session = AsyncMock()
    mock_session_service_cls.return_value = session_service

    event = MagicMock()
    event.is_final_response.return_value = True
    event.content = MagicMock()
    event.content.parts = [
        MagicMock(
            text=(
                '{"findings":[{"path":"service.py","line":11,"severity":"medium",'
                '"code":"missing-none-guard","category":"Correctness","confidence":"high",'
                '"message":"Guard payload.get(\\"items\\") before calling len(...) '
                'because it may be None.",'
                '"evidence":"The new code calls len(payload.get(\\"items\\")) directly,'
                ' and dict.get returns None when the key is absent.",'
                '"anchor":"count = len(payload.get(\\"items\\"))"}]}'
            )
        )
    ]
    runner = MagicMock()
    runner.run_async = runner_run_async_returning([event])
    mock_runner_cls.return_value = runner

    summary = run_local_golden_pr_review_eval(execution="adk")

    assert summary.suite_name == "golden_pr_review[adk]"
    assert summary.failed == 1
    assert summary.passed == 1
    mock_create_review_agent.assert_called()
    assert mock_runner_cls.call_count == summary.total
    for call in mock_runner_cls.call_args_list:
        assert call.kwargs == {
            "agent": mock_create_review_agent.return_value,
            "app_name": "code_review_eval",
            "session_service": session_service,
            "auto_create_session": True,
        }


@patch("google.adk.sessions.InMemorySessionService")
@patch("google.adk.runners.Runner")
@patch("code_review.evals.local_runner.create_reply_dismissal_agent")
def test_run_local_reply_dismissal_eval_adk_mode(
    mock_create_reply_agent, mock_runner_cls, mock_session_service_cls
) -> None:
    mock_create_reply_agent.return_value = MagicMock(name="reply_agent")
    session_service = MagicMock()
    session_service.create_session = AsyncMock()
    mock_session_service_cls.return_value = session_service

    event = MagicMock()
    event.is_final_response.return_value = True
    event.content = MagicMock()
    event.content.parts = [MagicMock(text='{"version":"1","verdict":"agreed","reply_text":""}')]
    runner = MagicMock()
    runner.run_async = runner_run_async_returning([event])
    mock_runner_cls.return_value = runner

    summary = run_local_reply_dismissal_eval(execution="adk")

    assert summary.suite_name == "reply_dismissal[adk]"
    assert summary.failed == 1
    assert summary.passed == 1
    mock_create_reply_agent.assert_called()
    assert mock_runner_cls.call_count == summary.total
    for call in mock_runner_cls.call_args_list:
        assert call.kwargs == {
            "agent": mock_create_reply_agent.return_value,
            "app_name": "code_review_eval",
            "session_service": session_service,
            "auto_create_session": True,
        }
