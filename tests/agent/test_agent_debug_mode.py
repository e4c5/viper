"""Tests for create_review_agent and instruction content."""

import asyncio
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_review.agent import (
    BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION,
    EMBEDDED_DIFF_REVIEW_INSTRUCTION,
    create_review_agent,
)
from code_review.agent.agent import (
    _after_model_callback,
    _before_model_callback,
)
from code_review.schemas.findings import FindingsBatchV1


# --- create_review_agent behaviour ---


@patch("google.adk.agents.Agent")
@patch("code_review.agent.agent.get_llm_config")
def test_create_review_agent_always_uses_no_tools(mock_get_llm_config, mock_agent_cls) -> None:
    """Agent is always created with an empty tools list."""
    provider = MagicMock()
    mock_get_llm_config.return_value = MagicMock(temperature=0.0, max_output_tokens=1024)
    mock_agent_cls.return_value = MagicMock()

    create_review_agent(provider)

    _, kwargs = mock_agent_cls.call_args
    assert kwargs["tools"] == []
    assert kwargs["output_schema"] is FindingsBatchV1


@patch("google.adk.agents.Agent")
@patch("code_review.agent.agent.get_llm_config")
def test_create_review_agent_default_uses_embedded_diff_instruction(
    mock_get_llm_config, mock_agent_cls
) -> None:
    """Default (slim_output=False) uses EMBEDDED_DIFF_REVIEW_INSTRUCTION."""
    provider = MagicMock()
    mock_get_llm_config.return_value = MagicMock(temperature=0.0, max_output_tokens=65_000)
    mock_agent_cls.return_value = MagicMock()

    create_review_agent(provider)

    _, kwargs = mock_agent_cls.call_args
    assert kwargs["instruction"] == EMBEDDED_DIFF_REVIEW_INSTRUCTION
    assert "get_file_content" not in kwargs["instruction"]
    assert "get_pr_diff_for_file" not in kwargs["instruction"]


@patch("google.adk.agents.Agent")
@patch("code_review.agent.agent.get_llm_config")
def test_create_review_agent_slim_output_uses_batch_instruction(
    mock_get_llm_config, mock_agent_cls
) -> None:
    """slim_output=True uses BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION."""
    provider = MagicMock()
    mock_get_llm_config.return_value = MagicMock(temperature=0.0, max_output_tokens=65_000)
    mock_agent_cls.return_value = MagicMock()

    create_review_agent(provider, slim_output=True)

    _, kwargs = mock_agent_cls.call_args
    assert kwargs["instruction"] == BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION
    assert "output_key" not in kwargs


@patch("google.adk.agents.Agent")
@patch("code_review.agent.agent.get_llm_config")
def test_create_review_agent_output_key_passed_when_set(
    mock_get_llm_config, mock_agent_cls
) -> None:
    """output_key is forwarded to Agent when provided."""
    provider = MagicMock()
    mock_get_llm_config.return_value = MagicMock(temperature=0.0, max_output_tokens=65_000)
    mock_agent_cls.return_value = MagicMock()

    create_review_agent(provider, slim_output=True, output_key="findings_result")

    _, kwargs = mock_agent_cls.call_args
    assert kwargs.get("output_key") == "findings_result"


@patch("google.adk.agents.Agent")
@patch("code_review.agent.agent.get_llm_config")
def test_create_review_agent_output_key_omitted_when_none(
    mock_get_llm_config, mock_agent_cls
) -> None:
    """output_key is not forwarded to Agent when None."""
    provider = MagicMock()
    mock_get_llm_config.return_value = MagicMock(temperature=0.0, max_output_tokens=65_000)
    mock_agent_cls.return_value = MagicMock()

    create_review_agent(provider)

    _, kwargs = mock_agent_cls.call_args
    assert "output_key" not in kwargs


@patch("google.adk.agents.Agent")
@patch("code_review.agent.agent.get_code_review_app_config")
@patch("code_review.agent.agent.get_llm_config")
def test_create_review_agent_adds_visible_lines_override_when_enabled(
    mock_get_llm_config, mock_get_app_cfg, mock_agent_cls
) -> None:
    provider = MagicMock()
    mock_get_llm_config.return_value = MagicMock(temperature=0.0, max_output_tokens=1024)
    mock_get_app_cfg.return_value = MagicMock(review_visible_lines=True)
    mock_agent_cls.return_value = MagicMock()

    create_review_agent(provider)

    _, kwargs = mock_agent_cls.call_args
    assert "LINE-SCOPE OVERRIDE:" in kwargs["instruction"]
    assert "including unchanged" in kwargs["instruction"]


# --- EMBEDDED_DIFF_REVIEW_INSTRUCTION content ---


def test_embedded_diff_review_instruction_category_field():
    """EMBEDDED_DIFF_REVIEW_INSTRUCTION must mention the category field with example values."""
    assert "category" in EMBEDDED_DIFF_REVIEW_INSTRUCTION
    assert (
        "Correctness" in EMBEDDED_DIFF_REVIEW_INSTRUCTION
        or "Security" in EMBEDDED_DIFF_REVIEW_INSTRUCTION
    )


# --- BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION content ---


def test_batch_embedded_diff_instruction_excludes_fix_guidance():
    """BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION must forbid suggested_patch and agent_fix_prompt."""
    instr = BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION
    assert "DO NOT include" in instr
    assert "highly recommended for fixable" not in instr
    assert "MANDATORY whenever" not in instr


def test_batch_embedded_diff_instruction_excludes_evidence_confidence():
    """BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION must not encourage evidence/confidence."""
    instr = BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION
    assert "Prefer including `evidence`" not in instr
    assert "Prefer including evidence" not in instr


def test_batch_embedded_diff_instruction_retains_required_fields():
    """BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION must require path, line, severity, code, message."""
    instr = BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION
    for field in ("path", "line", "severity", "code", "message"):
        assert field in instr, f"batch instruction missing required field: {field}"


def test_batch_embedded_diff_instruction_retains_analysis_methodology():
    """BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION must retain rigorous analysis guidance."""
    instr = BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION
    assert "Failure Mode Analysis" in instr
    assert "data flow" in instr


def test_batch_embedded_diff_instruction_no_tool_references():
    """BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION must not reference tools."""
    instr = BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION
    assert "get_file_content" not in instr
    assert "get_pr_diff_for_file" not in instr


# --- Shared fragment consistency ---


def test_shared_line_number_rules_appear_in_both_instructions():
    from code_review.agent.agent import _SHARED_LINE_NUMBER_RULES

    assert _SHARED_LINE_NUMBER_RULES in EMBEDDED_DIFF_REVIEW_INSTRUCTION
    assert _SHARED_LINE_NUMBER_RULES in BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION


def test_shared_format_and_placement_appears_in_embedded_instruction():
    from code_review.agent.agent import _SHARED_FORMAT_AND_PLACEMENT

    assert _SHARED_FORMAT_AND_PLACEMENT in EMBEDDED_DIFF_REVIEW_INSTRUCTION


def test_shared_agent_fix_and_examples_appears_in_embedded_instruction():
    from code_review.agent.agent import _SHARED_AGENT_FIX_AND_EXAMPLES

    assert _SHARED_AGENT_FIX_AND_EXAMPLES in EMBEDDED_DIFF_REVIEW_INSTRUCTION


# --- Model callbacks ---


def _run(coro):
    if inspect.isawaitable(coro):
        return asyncio.run(coro)
    return coro


class _FakeLlmRequest:
    def __init__(self) -> None:
        self.added_instructions: list[list[str]] = []

    def append_instructions(self, instructions: list[str]) -> None:
        self.added_instructions.append(instructions)


def test_before_model_callback_adds_no_tools_guardrail() -> None:
    llm_request = _FakeLlmRequest()
    _before_model_callback(SimpleNamespace(agent_name="code_review_agent"), llm_request)
    assert len(llm_request.added_instructions) == 1
    rendered = "\n".join(llm_request.added_instructions[0])
    assert "No tools are available for this run" in rendered
    assert "required structured schema" in rendered


def test_after_model_callback_logs_usage_metadata(caplog) -> None:
    llm_response = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=123,
            candidates_token_count=45,
            total_token_count=168,
            cached_content_token_count=7,
            tool_use_prompt_token_count=8,
            thoughts_token_count=9,
        ),
        content=SimpleNamespace(parts=[]),
    )

    caplog.set_level(logging.INFO, logger="code_review.agent.agent")
    _after_model_callback(SimpleNamespace(agent_name="batch_review_0"), llm_response)

    assert "LLM usage agent=batch_review_0" in caplog.text
    assert "prompt_tokens=123" in caplog.text
    assert "completion_tokens=45" in caplog.text
    assert "total_tokens=168" in caplog.text
    assert "finish_reason=" in caplog.text
    assert "response_text_len=0" in caplog.text


def test_after_model_callback_logs_finish_reason_when_present(caplog) -> None:
    llm_response = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=10,
            candidates_token_count=5,
            total_token_count=15,
            cached_content_token_count=0,
            tool_use_prompt_token_count=0,
            thoughts_token_count=0,
        ),
        finish_reason="MAX_TOKENS",
        interrupted=True,
        turn_complete=False,
        content=SimpleNamespace(parts=[SimpleNamespace(text='{"findings":[]}}')]),
    )

    caplog.set_level(logging.INFO, logger="code_review.agent.agent")
    _after_model_callback(SimpleNamespace(agent_name="batch_review_0"), llm_response)

    assert "finish_reason=MAX_TOKENS" in caplog.text
    assert "interrupted=True" in caplog.text
    assert "turn_complete=False" in caplog.text
