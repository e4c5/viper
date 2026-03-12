"""Tests for model factory and context helpers."""

import os
from unittest.mock import MagicMock, patch

from code_review.models import (
    get_configured_model,
    get_context_window,
    get_max_output_tokens,
)


@patch("code_review.models.get_llm_config")
def test_get_configured_model_gemini_returns_model_string(mock_get_config):
    mock_get_config.return_value = MagicMock(
        provider="gemini", model="gemini-2.0-flash", api_key=None
    )
    result = get_configured_model()
    assert result == "gemini-2.0-flash"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_vertex_returns_model_string(mock_get_config):
    mock_get_config.return_value = MagicMock(
        provider="vertex", model="gemini-1.5-pro", api_key=None
    )
    result = get_configured_model()
    assert result == "gemini-1.5-pro"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_openai_uses_litellm_or_fallback(mock_get_config):
    mock_get_config.return_value = MagicMock(
        provider="openai", model="gpt-4o", api_key=None
    )
    result = get_configured_model()
    # Either LiteLlm instance or model string if ImportError
    if hasattr(result, "model"):
        assert result.model == "openai/gpt-4o"
    else:
        assert result == "gpt-4o"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_openrouter_uses_litellm_or_fallback(mock_get_config):
    mock_get_config.return_value = MagicMock(
        provider="openrouter", model="gpt-4.1-mini", api_key=None
    )
    result = get_configured_model()
    # Either LiteLlm instance or model string if ImportError
    if hasattr(result, "model"):
        assert result.model == "openrouter/gpt-4.1-mini"
    else:
        assert result == "gpt-4.1-mini"


@patch("code_review.models.get_llm_config")
def test_get_context_window(mock_get_config):
    mock_get_config.return_value = MagicMock(
        context_window=64_000, api_key=None
    )
    assert get_context_window() == 64_000


@patch("code_review.models.get_llm_config")
def test_get_max_output_tokens(mock_get_config):
    mock_get_config.return_value = MagicMock(
        max_output_tokens=2048, api_key=None
    )
    assert get_max_output_tokens() == 2048


@patch("code_review.models.get_llm_config")
def test_get_configured_model_sets_provider_env_var_from_llm_api_key(mock_get_config):
    """When LLM_API_KEY is set, get_configured_model() sets the provider-specific env var."""
    from pydantic import SecretStr

    mock_get_config.return_value = MagicMock(
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        api_key=SecretStr("sk-fake"),
    )
    try:
        result = get_configured_model()
        assert os.environ.get("OPENROUTER_API_KEY") == "sk-fake"
        if hasattr(result, "model"):
            assert result.model == "openrouter/anthropic/claude-3.5-sonnet"
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)
