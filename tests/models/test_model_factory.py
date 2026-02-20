"""Tests for model factory and context helpers."""

from unittest.mock import MagicMock, patch

from code_review.models import (
    get_configured_model,
    get_context_window,
    get_max_output_tokens,
)


@patch("code_review.models.get_llm_config")
def test_get_configured_model_gemini_returns_model_string(mock_get_config):
    mock_get_config.return_value = MagicMock(provider="gemini", model="gemini-2.0-flash")
    result = get_configured_model()
    assert result == "gemini-2.0-flash"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_vertex_returns_model_string(mock_get_config):
    mock_get_config.return_value = MagicMock(provider="vertex", model="gemini-1.5-pro")
    result = get_configured_model()
    assert result == "gemini-1.5-pro"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_openai_uses_litellm_or_fallback(mock_get_config):
    mock_get_config.return_value = MagicMock(provider="openai", model="gpt-4o")
    result = get_configured_model()
    # Either LiteLlm instance or model string if ImportError
    if hasattr(result, "model"):
        assert result.model == "openai/gpt-4o"
    else:
        assert result == "gpt-4o"


@patch("code_review.models.get_llm_config")
def test_get_context_window(mock_get_config):
    mock_get_config.return_value = MagicMock(context_window=64_000)
    assert get_context_window() == 64_000


@patch("code_review.models.get_llm_config")
def test_get_max_output_tokens(mock_get_config):
    mock_get_config.return_value = MagicMock(max_output_tokens=2048)
    assert get_max_output_tokens() == 2048
