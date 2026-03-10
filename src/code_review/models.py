"""Model factory and context size mapping. Returns configured LLM instance."""

from typing import Any

from code_review.config import get_llm_config


def get_configured_model() -> Any:
    """
    Return the configured LLM instance for ADK.
    Reads LLM_PROVIDER and LLM_MODEL from env/config.
    Uses LiteLLM for OpenAI/Anthropic/Ollama/OpenRouter; string for Gemini/Vertex (ADK registry).
    """
    config = get_llm_config()

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
