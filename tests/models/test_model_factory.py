"""Tests for model factory and context helpers."""

import os
from unittest.mock import MagicMock, patch

import pytest

import code_review.models as model_factory
from code_review.models import (
    get_configured_model,
    get_context_window,
    get_max_output_tokens,
)


@pytest.fixture(autouse=True)
def _reset_injected_env_tracking():
    """Ensure module-level env-injection tracking does not leak across tests."""
    model_factory._INJECTED_PROVIDER_API_ENV = None
    model_factory._PREVIOUS_PROVIDER_API_VALUE = None
    yield
    model_factory._INJECTED_PROVIDER_API_ENV = None
    model_factory._PREVIOUS_PROVIDER_API_VALUE = None


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
def test_get_configured_model_anthropic_uses_litellm_or_fallback(mock_get_config):
    mock_get_config.return_value = MagicMock(
        provider="anthropic", model="claude-3-5-sonnet-20241022", api_key=None
    )
    result = get_configured_model()
    if hasattr(result, "model"):
        assert result.model == "anthropic/claude-3-5-sonnet-20241022"
    else:
        assert "claude" in str(result)


@patch("code_review.models.get_llm_config")
def test_get_configured_model_ollama_uses_litellm_or_fallback(mock_get_config):
    mock_get_config.return_value = MagicMock(
        provider="ollama", model="llama3.2", api_key=None
    )
    result = get_configured_model()
    if hasattr(result, "model"):
        assert result.model == "ollama_chat/llama3.2"
    else:
        assert result == "llama3.2"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_litellm_import_error_returns_model_string(mock_get_config):
    """When LiteLlm cannot be imported, return config.model as fallback."""
    import builtins

    mock_get_config.return_value = MagicMock(
        provider="openrouter", model="openai/gpt-4o", api_key=None
    )
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "google.adk.models.lite_llm":
            raise ImportError("no lite_llm")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        result = get_configured_model()
    assert result == "openai/gpt-4o"


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
def test_get_configured_model_unknown_provider_uses_model_string_as_litellm(mock_get_config):
    """Unknown provider falls through to else: litellm_model = config.model."""
    mock_get_config.return_value = MagicMock(
        provider="custom", model="custom/model-name", api_key=None
    )
    result = get_configured_model()
    # Either LiteLlm(custom/model-name) or fallback string
    if hasattr(result, "model"):
        assert result.model == "custom/model-name"
    else:
        assert result == "custom/model-name"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_sets_provider_env_var_from_llm_api_key(mock_get_config):
    """When LLM_API_KEY is set, get_configured_model() sets the provider-specific env var."""
    from pydantic import SecretStr

    mock_get_config.return_value = MagicMock(
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        api_key=SecretStr("sk-fake"),
    )
    previous_api_key = os.environ.get("OPENROUTER_API_KEY")
    try:
        result = get_configured_model()
        assert os.environ.get("OPENROUTER_API_KEY") == "sk-fake"
        if hasattr(result, "model"):
            assert result.model == "openrouter/anthropic/claude-3.5-sonnet"
    finally:
        if previous_api_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = previous_api_key


@patch("code_review.models.get_llm_config")
def test_get_configured_model_ignores_blank_api_key(mock_get_config):
    """Blank API keys must not overwrite provider-specific credentials."""
    from pydantic import SecretStr

    previous_api_key = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = "existing-token"
    mock_get_config.return_value = MagicMock(
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        api_key=SecretStr("   "),
    )
    try:
        get_configured_model()
        assert os.environ.get("OPENROUTER_API_KEY") == "existing-token"
    finally:
        if previous_api_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = previous_api_key


@patch("code_review.models.get_llm_config")
def test_get_configured_model_clears_injected_key_on_provider_switch(mock_get_config):
    """Injected provider key should not leak after switching providers."""
    from pydantic import SecretStr

    previous_openrouter = os.environ.get("OPENROUTER_API_KEY")
    previous_openai = os.environ.get("OPENAI_API_KEY")
    mock_get_config.side_effect = [
        MagicMock(provider="openrouter", model="claude", api_key=SecretStr("sk-openrouter")),
        MagicMock(provider="openai", model="gpt-4o", api_key=SecretStr("sk-openai")),
    ]
    try:
        get_configured_model()
        assert os.environ.get("OPENROUTER_API_KEY") == "sk-openrouter"

        get_configured_model()
        if previous_openrouter is None:
            assert "OPENROUTER_API_KEY" not in os.environ
        else:
            assert os.environ.get("OPENROUTER_API_KEY") == previous_openrouter
        assert os.environ.get("OPENAI_API_KEY") == "sk-openai"
    finally:
        if previous_openrouter is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = previous_openrouter
        if previous_openai is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = previous_openai
