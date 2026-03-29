"""Tests for model factory and context helpers."""

import os
from unittest.mock import MagicMock, patch

import pytest

import code_review.models as model_factory
from code_review.models import (
    get_configured_model,
    get_context_window,
    get_max_output_tokens,
    get_model_metadata,
    get_model_metadata_catalog,
    get_model_token_costs,
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
    mock_get_config.return_value = MagicMock(provider="openai", model="gpt-4o", api_key=None)
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
    mock_get_config.return_value = MagicMock(provider="ollama", model="llama3.2", api_key=None)
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
    mock_get_config.return_value = MagicMock(context_window=64_000, api_key=None)
    assert get_context_window() == 64_000


@patch("code_review.models.get_llm_config")
def test_get_max_output_tokens(mock_get_config):
    mock_get_config.return_value = MagicMock(max_output_tokens=2048, api_key=None)
    assert get_max_output_tokens() == 2048


def test_get_model_metadata_catalog_returns_copy():
    catalog = get_model_metadata_catalog()

    assert ("openai", "gpt-4.1") in catalog
    del catalog[("openai", "gpt-4.1")]

    assert ("openai", "gpt-4.1") in get_model_metadata_catalog()


def test_get_model_metadata_known_pair():
    metadata = get_model_metadata("openai", "gpt-4.1-mini")

    assert metadata is not None
    assert metadata.context_window_tokens == 1_047_576
    assert metadata.max_output_tokens_default == 32_768
    assert metadata.input_cost_per_million_tokens == pytest.approx(0.40)
    assert metadata.output_cost_per_million_tokens == pytest.approx(1.60)
    assert metadata.source_url.startswith("https://")
    assert metadata.verified_on == "2026-03-29"


def test_get_model_metadata_refreshed_gemini_limits():
    metadata = get_model_metadata("gemini", "gemini-3.1")

    assert metadata is not None
    assert metadata.context_window_tokens == 1_000_000
    assert metadata.max_output_tokens_default == 64_000


@patch("code_review.models.get_llm_config")
def test_get_configured_model_gemini_alias_resolves_to_runtime_model(mock_get_config):
    mock_get_config.return_value = MagicMock(provider="gemini", model="gemini-3.1", api_key=None)

    assert get_configured_model() == "gemini-3-flash-preview"


@patch("code_review.models.get_llm_config")
def test_get_model_metadata_uses_config_when_args_omitted(mock_get_config):
    mock_get_config.return_value = MagicMock(
        provider="anthropic",
        model="claude-3-5-sonnet-latest",
        api_key=None,
    )

    metadata = get_model_metadata()

    assert metadata is not None
    assert metadata.provider == "anthropic"
    assert metadata.model == "claude-3-5-sonnet-latest"


def test_get_model_token_costs_known_pair():
    assert get_model_token_costs("openai", "gpt-4o") == pytest.approx((2.50, 10.00))


def test_get_model_token_costs_unknown_pair():
    assert get_model_token_costs("custom", "unknown-model") == (None, None)


@patch("code_review.models.get_llm_config")
def test_get_context_window_uses_metadata_fallback(mock_get_config, monkeypatch):
    monkeypatch.delenv("LLM_CONTEXT_WINDOW", raising=False)
    mock_get_config.return_value = MagicMock(
        provider="openai",
        model="gpt-4.1",
        context_window=64_000,
        api_key=None,
    )

    assert get_context_window() == 1_047_576


@patch("code_review.models.get_llm_config")
def test_get_context_window_respects_explicit_env_override(mock_get_config, monkeypatch):
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "777777")
    mock_get_config.return_value = MagicMock(
        provider="openai",
        model="gpt-4.1",
        context_window=777_777,
        api_key=None,
    )

    assert get_context_window() == 777_777


@patch("code_review.models.get_llm_config")
def test_get_context_window_unknown_model_falls_back_to_config(mock_get_config, monkeypatch):
    monkeypatch.delenv("LLM_CONTEXT_WINDOW", raising=False)
    mock_get_config.return_value = MagicMock(
        provider="custom",
        model="unknown-model",
        context_window=123_456,
        api_key=None,
    )

    assert get_context_window() == 123_456


@patch("code_review.models.get_llm_config")
def test_get_max_output_tokens_uses_metadata_fallback(mock_get_config, monkeypatch):
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)
    mock_get_config.return_value = MagicMock(
        provider="openai",
        model="gpt-4.1-mini",
        max_output_tokens=4096,
        api_key=None,
    )

    assert get_max_output_tokens() == 32_768


@patch("code_review.models.get_llm_config")
def test_get_max_output_tokens_respects_explicit_env_override(mock_get_config, monkeypatch):
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "8192")
    mock_get_config.return_value = MagicMock(
        provider="openai",
        model="gpt-4.1-mini",
        max_output_tokens=8192,
        api_key=None,
    )

    assert get_max_output_tokens() == 8192


@patch("code_review.models.get_llm_config")
def test_get_max_output_tokens_unknown_model_falls_back_to_config(mock_get_config, monkeypatch):
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)
    mock_get_config.return_value = MagicMock(
        provider="custom",
        model="unknown-model",
        max_output_tokens=3072,
        api_key=None,
    )

    assert get_max_output_tokens() == 3072


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
