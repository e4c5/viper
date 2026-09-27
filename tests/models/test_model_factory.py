"""Tests for model factory and context helpers."""

import os
from unittest.mock import MagicMock, patch

import pytest

from code_review.models import (
    PRContext,
    get_configured_model,
    get_configured_summary_model,
    get_configured_verification_model,
    get_context_window,
    get_effective_temperature_for_model,
    get_max_output_tokens,
    get_model_metadata,
    get_model_metadata_catalog,
    get_model_token_costs,
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
def test_get_configured_model_litellm_import_error_raises_clear_error(mock_get_config):
    """Non-gemini providers without the litellm extra must fail with guidance."""
    import builtins

    mock_get_config.return_value = MagicMock(
        provider="openrouter", model="openai/gpt-4o", api_key=None
    )
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "google.adk.models.lite_llm":
            raise ImportError("no lite_llm")
        return real_import(name, *args, **kwargs)

    with (
        patch("builtins.__import__", side_effect=fake_import),
        pytest.raises(ImportError, match="litellm"),
    ):
        get_configured_model()


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
def test_get_configured_model_deepseek_uses_litellm_or_fallback(mock_get_config):
    mock_get_config.return_value = MagicMock(
        provider="deepseek", model="deepseek-chat", api_key=None
    )
    result = get_configured_model()
    # Either LiteLlm instance or model string if ImportError
    if hasattr(result, "model"):
        assert result.model == "deepseek/deepseek-chat"
    else:
        assert result == "deepseek-chat"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_deepseek_passes_api_key_per_instance(mock_get_config):
    from pydantic import SecretStr

    mock_get_config.return_value = MagicMock(
        provider="deepseek", model="deepseek-chat", api_key=SecretStr("sk-deepseek")
    )
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        result = get_configured_model()
        assert "DEEPSEEK_API_KEY" not in os.environ
    assert result._additional_args.get("api_key") == "sk-deepseek"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_deepseek_caps_reasoning_effort(mock_get_config):
    """DeepSeek's reasoning can consume the whole output budget before emitting
    JSON; capping reasoning_effort leaves reliable headroom for the answer."""
    mock_get_config.return_value = MagicMock(
        provider="deepseek", model="deepseek-v4-pro", api_key=None
    )
    result = get_configured_model()
    if hasattr(result, "_additional_args"):
        assert result._additional_args.get("reasoning_effort") == "high"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_non_deepseek_omits_reasoning_effort(mock_get_config):
    mock_get_config.return_value = MagicMock(
        provider="openrouter", model="gpt-4.1-mini", api_key=None
    )
    result = get_configured_model()
    if hasattr(result, "_additional_args"):
        assert "reasoning_effort" not in result._additional_args


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
    assert metadata.context_window_tokens == 200_000
    assert metadata.max_output_tokens_default == 65_536


def test_get_model_metadata_deepseek_v4_pro_raised_output_budget():
    """max_output_tokens_default must exceed the 4096 config default by a wide
    margin: DeepSeek's reasoning alone consumed 2400-3700 tokens in testing,
    leaving too little room for the JSON answer at the old default."""
    metadata = get_model_metadata("deepseek", "deepseek-v4-pro")

    assert metadata is not None
    assert metadata.max_output_tokens_default >= 16_000


def test_pr_context_gitlab_url_strips_api_prefix():
    cfg = MagicMock(provider="gitlab", url="https://gitlab.example.com/api/v4")

    pr_url = PRContext("group/subgroup", "demo", 7).pr_url(cfg)

    assert pr_url == "https://gitlab.example.com/group/subgroup/demo/-/merge_requests/7"


def test_get_effective_temperature_for_model_omits_fixed_temperature_models():
    assert get_effective_temperature_for_model("openai", "gpt-5.4", 0.2) is None


def test_get_effective_temperature_for_model_keeps_regular_models():
    assert get_effective_temperature_for_model("gemini", "gemini-3.1", 0.2) == pytest.approx(
        0.2
    )


@patch("code_review.models.get_llm_config")
def test_get_configured_model_gemini_alias_resolves_to_runtime_model(mock_get_config):
    mock_get_config.return_value = MagicMock(provider="gemini", model="gemini-3.1", api_key=None)

    assert get_configured_model() == "gemini-3-flash-preview"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_gemini_with_key_returns_keyed_instance(mock_get_config):
    """Gemini + API key -> ADK Gemini with client_kwargs api_key; env untouched."""
    from pydantic import SecretStr

    mock_get_config.return_value = MagicMock(
        provider="gemini", model="gemini-2.0-flash", api_key=SecretStr("g-key")
    )
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GOOGLE_API_KEY", None)
        result = get_configured_model()
        assert "GOOGLE_API_KEY" not in os.environ
    assert result.model == "gemini-2.0-flash"
    assert result.client_kwargs.get("api_key") == "g-key"


@patch("code_review.models.get_summary_llm_config")
@patch("code_review.models.get_llm_config")
def test_get_configured_summary_model_falls_back_to_primary(mock_get_config, mock_get_summary):
    from pydantic import SecretStr

    mock_get_config.return_value = MagicMock(
        provider="gemini",
        model="gemini-3.1",
        api_key=SecretStr("primary-key"),
    )
    mock_get_summary.return_value = MagicMock(provider=None, model=None, api_key=None)

    result = get_configured_summary_model()
    assert result.model == "gemini-3-flash-preview"
    assert result.client_kwargs.get("api_key") == "primary-key"


@patch("code_review.models.get_summary_llm_config")
@patch("code_review.models.get_llm_config")
def test_get_configured_summary_model_uses_task_override(mock_get_config, mock_get_summary):
    mock_get_config.return_value = MagicMock(provider="gemini", model="gemini-3.1", api_key=None)
    mock_get_summary.return_value = MagicMock(
        provider="gemini",
        model="gemini-3-flash-lite-preview",
        api_key=None,
    )

    assert get_configured_summary_model() == "gemini-3-flash-lite-preview"


@patch("code_review.models.get_verification_llm_config")
@patch("code_review.models.get_llm_config")
def test_get_configured_verification_model_falls_back_to_primary(
    mock_get_config, mock_get_verification
):
    mock_get_config.return_value = MagicMock(provider="gemini", model="gemini-3.1", api_key=None)
    mock_get_verification.return_value = MagicMock(provider=None, model=None, api_key=None)

    assert get_configured_verification_model() == "gemini-3-flash-preview"


@patch("code_review.models.get_verification_llm_config")
@patch("code_review.models.get_llm_config")
def test_get_configured_verification_model_uses_task_override(
    mock_get_config, mock_get_verification
):
    mock_get_config.return_value = MagicMock(provider="gemini", model="gemini-3.1", api_key=None)
    mock_get_verification.return_value = MagicMock(
        provider="openai",
        model="gpt-5-mini",
        api_key=None,
    )

    result = get_configured_verification_model()
    if hasattr(result, "model"):
        assert result.model == "openai/gpt-5-mini"
    else:
        assert result == "gpt-5-mini"


@patch("code_review.models.get_summary_llm_config")
@patch("code_review.models.get_llm_config")
def test_get_configured_summary_model_uses_task_api_key(mock_get_config, mock_get_summary):
    from pydantic import SecretStr

    mock_get_config.return_value = MagicMock(
        provider="gemini",
        model="gemini-3.1",
        api_key=SecretStr("primary-key"),
    )
    mock_get_summary.return_value = MagicMock(
        provider="openrouter",
        model="google/gemini-3-flash-lite-preview",
        api_key=SecretStr("summary-key"),
    )
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENROUTER_API_KEY", None)
        result = get_configured_summary_model()
        assert "OPENROUTER_API_KEY" not in os.environ
    assert result.model == "openrouter/google/gemini-3-flash-lite-preview"
    assert result._additional_args.get("api_key") == "summary-key"


@patch("code_review.models.get_verification_llm_config")
@patch("code_review.models.get_llm_config")
def test_get_configured_verification_model_falls_back_to_primary_api_key(
    mock_get_config, mock_get_verification
):
    from pydantic import SecretStr

    mock_get_config.return_value = MagicMock(
        provider="openai",
        model="gpt-5-mini",
        api_key=SecretStr("primary-key"),
    )
    mock_get_verification.return_value = MagicMock(provider=None, model=None, api_key=None)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENAI_API_KEY", None)
        result = get_configured_verification_model()
        assert "OPENAI_API_KEY" not in os.environ
    assert result._additional_args.get("api_key") == "primary-key"


@patch("code_review.models.get_summary_llm_config")
@patch("code_review.models.get_llm_config")
def test_get_configured_summary_model_does_not_reuse_primary_api_key_for_different_provider(
    mock_get_config, mock_get_summary
):
    from pydantic import SecretStr

    mock_get_config.return_value = MagicMock(
        provider="openai",
        model="gpt-5-mini",
        api_key=SecretStr("openai-key"),
    )
    mock_get_summary.return_value = MagicMock(
        provider="gemini",
        model="gemini-3.1",
        api_key=None,
    )
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_API_KEY", None)
        result = get_configured_summary_model()
        assert "GEMINI_API_KEY" not in os.environ
        assert "GOOGLE_API_KEY" not in os.environ
    # Task override on a different provider must not reuse the primary key:
    # with no key of its own, gemini falls back to env/ADC (plain model string).
    assert result == "gemini-3-flash-preview"


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
def test_get_configured_model_passes_api_key_per_instance(mock_get_config):
    """LLM_API_KEY goes to the LiteLlm instance, never os.environ."""
    from pydantic import SecretStr

    mock_get_config.return_value = MagicMock(
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        api_key=SecretStr("sk-fake"),
    )
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENROUTER_API_KEY", None)
        result = get_configured_model()
        assert "OPENROUTER_API_KEY" not in os.environ
    assert result.model == "openrouter/anthropic/claude-3.5-sonnet"
    assert result._additional_args.get("api_key") == "sk-fake"


@patch("code_review.models.get_llm_config")
def test_get_configured_model_ignores_blank_api_key(mock_get_config):
    """Blank API keys produce no per-instance credential."""
    from pydantic import SecretStr

    mock_get_config.return_value = MagicMock(
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        api_key=SecretStr("   "),
    )
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "existing-token"}, clear=False):
        result = get_configured_model()
        assert os.environ.get("OPENROUTER_API_KEY") == "existing-token"
    assert "api_key" not in result._additional_args


@patch("code_review.models.get_llm_config")
def test_get_configured_model_two_keys_no_env_mutation(mock_get_config):
    """Two providers with different keys: each instance carries its own key and
    os.environ is never touched."""
    from pydantic import SecretStr

    mock_get_config.side_effect = [
        MagicMock(provider="openrouter", model="claude", api_key=SecretStr("sk-openrouter")),
        MagicMock(provider="openai", model="gpt-4o", api_key=SecretStr("sk-openai")),
    ]
    with patch.dict(
        os.environ,
        {
            "OPENROUTER_API_KEY": "previous-openrouter",
            "OPENAI_API_KEY": "previous-openai",
        },
        clear=False,
    ):
        first = get_configured_model()
        second = get_configured_model()
        assert os.environ.get("OPENROUTER_API_KEY") == "previous-openrouter"
        assert os.environ.get("OPENAI_API_KEY") == "previous-openai"
    assert first._additional_args.get("api_key") == "sk-openrouter"
    assert second._additional_args.get("api_key") == "sk-openai"
