"""Model factory and context size mapping. Returns configured LLM instance."""

import os
from typing import Any

from code_review.config import get_llm_config

# Env var name per provider (used when LLM_API_KEY is set; Ollama has no key).
_PROVIDER_API_KEY_ENV: dict[str, str] = {
    "gemini": "GOOGLE_API_KEY",
    "vertex": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

_INJECTED_PROVIDER_API_ENV: str | None = None
_PREVIOUS_PROVIDER_API_VALUE: str | None = None


def _clear_injected_provider_api_env() -> None:
    """Undo provider-key env var injection performed by this module."""
    global _INJECTED_PROVIDER_API_ENV, _PREVIOUS_PROVIDER_API_VALUE
    if _INJECTED_PROVIDER_API_ENV is None:
        return
    if _PREVIOUS_PROVIDER_API_VALUE is None:
        os.environ.pop(_INJECTED_PROVIDER_API_ENV, None)
    else:
        os.environ[_INJECTED_PROVIDER_API_ENV] = _PREVIOUS_PROVIDER_API_VALUE
    _INJECTED_PROVIDER_API_ENV = None
    _PREVIOUS_PROVIDER_API_VALUE = None


def get_configured_model() -> Any:
    """
    Return the configured LLM instance for ADK.
    Reads LLM_PROVIDER, LLM_MODEL, and LLM_API_KEY from env/config.
    When LLM_API_KEY is set, it is applied to the provider-specific env var so ADK/LiteLLM see it.
    Uses LiteLLM for OpenAI/Anthropic/Ollama/OpenRouter; string for Gemini/Vertex (ADK registry).
    """
    global _INJECTED_PROVIDER_API_ENV, _PREVIOUS_PROVIDER_API_VALUE

    config = get_llm_config()
    env_var = _PROVIDER_API_KEY_ENV.get(config.provider)
    api_key = config.api_key.get_secret_value().strip() if config.api_key is not None else ""

    # Keep injected provider credentials scoped to the current config/provider call.
    if _INJECTED_PROVIDER_API_ENV and (_INJECTED_PROVIDER_API_ENV != env_var or not api_key):
        _clear_injected_provider_api_env()

    if env_var and api_key:
        if _INJECTED_PROVIDER_API_ENV != env_var:
            _PREVIOUS_PROVIDER_API_VALUE = os.environ.get(env_var)
        os.environ[env_var] = api_key
        _INJECTED_PROVIDER_API_ENV = env_var

    if config.provider in {"gemini", "vertex"}:
        return config.model
    # Use LiteLLM for OpenAI, Anthropic, Ollama, OpenRouter
    if config.provider == "openai":
        litellm_model = f"openai/{config.model}"
    elif config.provider == "anthropic":
        litellm_model = f"anthropic/{config.model}"
    elif config.provider == "ollama":
        litellm_model = f"ollama_chat/{config.model}"
    elif config.provider == "openrouter":
        litellm_model = f"openrouter/{config.model}"
    else:
        litellm_model = config.model

    try:
        from google.adk.models.lite_llm import LiteLlm

        return LiteLlm(model=litellm_model)
    except ImportError:
        # Fallback if ADK LiteLLM not available
        return config.model


def get_context_window() -> int:
    """
    Return context window size in tokens for runner chunking.
    Uses LLM_CONTEXT_WINDOW env or config; explicit, no model-name guessing.
    """
    config = get_llm_config()
    return config.context_window


def get_max_output_tokens() -> int:
    """Return max output tokens from config."""
    config = get_llm_config()
    return config.max_output_tokens
