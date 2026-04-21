"""Tests for normalized LLM telemetry logging."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from code_review.llm_telemetry import (
    effective_llm_identity,
    log_llm_usage,
    usage_from_litellm_response,
)


def test_effective_llm_identity_uses_task_override():
    with (
        patch(
            "code_review.llm_telemetry.get_llm_config",
            return_value=SimpleNamespace(provider="gemini", model="gemini-3.1"),
        ),
        patch(
            "code_review.llm_telemetry.get_summary_llm_config",
            return_value=SimpleNamespace(provider="anthropic", model="claude-sonnet-4-5"),
        ),
    ):
        assert effective_llm_identity("summary") == ("anthropic", "claude-sonnet-4-5")


def test_effective_llm_identity_falls_back_to_primary_for_task():
    with (
        patch(
            "code_review.llm_telemetry.get_llm_config",
            return_value=SimpleNamespace(provider="gemini", model="gemini-3.1"),
        ),
        patch(
            "code_review.llm_telemetry.get_verification_llm_config",
            return_value=SimpleNamespace(provider=None, model=None),
        ),
    ):
        assert effective_llm_identity("verification") == ("gemini", "gemini-3.1")


def test_log_llm_usage_normalizes_adk_usage(caplog):
    usage = SimpleNamespace(
        prompt_token_count=10,
        candidates_token_count=5,
        total_token_count=15,
        cached_content_token_count=1,
        tool_use_prompt_token_count=2,
        thoughts_token_count=3,
    )
    logger = logging.getLogger("code_review.test_llm_telemetry")

    caplog.set_level(logging.INFO)
    logger.setLevel(logging.INFO)
    log_llm_usage(
        logger,
        task="summary",
        provider="gemini",
        model="gemini-3.1",
        usage=usage,
        response_text_len=42,
        finish_reason="STOP",
    )

    assert "llm_usage task=summary provider=gemini model=gemini-3.1" in caplog.text
    assert "prompt_tokens=10" in caplog.text
    assert "completion_tokens=5" in caplog.text
    assert "total_tokens=15" in caplog.text
    assert "response_text_len=42" in caplog.text


def test_log_llm_usage_normalizes_litellm_usage(caplog):
    logger = logging.getLogger("code_review.test_litellm_telemetry")

    caplog.set_level(logging.INFO)
    logger.setLevel(logging.INFO)
    log_llm_usage(
        logger,
        task="semantic_query",
        provider="openai",
        model="gpt-5.4",
        usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    )

    assert "llm_usage task=semantic_query provider=openai model=gpt-5.4" in caplog.text
    assert "prompt_tokens=11" in caplog.text
    assert "completion_tokens=7" in caplog.text
    assert "total_tokens=18" in caplog.text


def test_usage_from_litellm_response_supports_dict_and_object():
    assert usage_from_litellm_response({"usage": {"total_tokens": 3}}) == {"total_tokens": 3}
    response = MagicMock()
    response.usage = {"total_tokens": 4}
    assert usage_from_litellm_response(response) == {"total_tokens": 4}
