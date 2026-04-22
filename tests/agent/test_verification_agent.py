"""Tests for verification agent factory."""

from unittest.mock import MagicMock, patch

import pytest

from code_review.agent.verification_agent import create_verification_agent


@patch("code_review.models.get_configured_verification_model")
@patch("code_review.config.get_verification_llm_config")
@patch("code_review.config.get_llm_config")
@patch("google.adk.agents.Agent")
def test_create_verification_agent_uses_verification_model_helper(
    mock_agent_cls, mock_get_llm_cfg, mock_get_verification_cfg, mock_get_verification_model
):
    mock_get_llm_cfg.return_value = MagicMock(
        provider="gemini", model="gemini-3.1", max_output_tokens=8192
    )
    mock_get_verification_cfg.return_value = MagicMock(provider=None, model=None)
    mock_get_verification_model.return_value = "cheap-verification-model"
    inst = MagicMock()
    mock_agent_cls.return_value = inst

    out = create_verification_agent()

    assert out is inst
    _, kwargs = mock_agent_cls.call_args
    assert kwargs["model"] == "cheap-verification-model"
    assert kwargs["name"] == "verification_agent"
    assert kwargs["generate_content_config"].temperature == pytest.approx(0.1)
    mock_get_verification_model.assert_called_once()


@patch("code_review.agent.verification_agent.log_adk_llm_usage")
@patch("code_review.models.get_configured_verification_model")
@patch("code_review.config.get_verification_llm_config")
@patch("code_review.config.get_llm_config")
@patch("google.adk.agents.Agent")
def test_create_verification_agent_after_model_callback_accepts_adk_keywords(
    mock_agent_cls,
    mock_get_llm_cfg,
    mock_get_verification_cfg,
    mock_get_verification_model,
    mock_log_usage,
):
    mock_get_llm_cfg.return_value = MagicMock(
        provider="gemini", model="gemini-3.1", max_output_tokens=8192
    )
    mock_get_verification_cfg.return_value = MagicMock(provider=None, model=None)
    mock_get_verification_model.return_value = "cheap-verification-model"

    create_verification_agent()

    _, kwargs = mock_agent_cls.call_args
    response = MagicMock()
    kwargs["after_model_callback"](callback_context=MagicMock(), llm_response=response)

    mock_log_usage.assert_called_once()
    _, log_kwargs = mock_log_usage.call_args
    assert log_kwargs["task"] == "verification"
    assert log_kwargs["response"] is response
    assert log_kwargs["provider"] == "gemini"
    assert log_kwargs["model"] == "gemini-3.1"


@patch("code_review.models.get_configured_verification_model")
@patch("code_review.config.get_verification_llm_config")
@patch("code_review.config.get_llm_config")
@patch("google.adk.agents.Agent")
def test_create_verification_agent_omits_temperature_for_fixed_temperature_override(
    mock_agent_cls, mock_get_llm_cfg, mock_get_verification_cfg, mock_get_verification_model
):
    mock_get_llm_cfg.return_value = MagicMock(
        provider="gemini", model="gemini-3.1", max_output_tokens=8192
    )
    mock_get_verification_cfg.return_value = MagicMock(provider="openai", model="gpt-5.4")
    mock_get_verification_model.return_value = "openai/gpt-5.4"

    create_verification_agent()

    _, kwargs = mock_agent_cls.call_args
    assert kwargs["generate_content_config"].temperature is None
