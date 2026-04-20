"""Model factory and model metadata helpers."""

import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from pydantic import SecretStr

from code_review.config import get_llm_config, get_summary_llm_config, get_verification_llm_config

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
_MODEL_METADATA_FILENAME = "model_metadata.json"
_MODEL_ALIASES: dict[tuple[str, str], str] = {
    ("gemini", "gemini-3.1"): "gemini-3-flash-preview",
    ("vertex", "gemini-3.1"): "gemini-3-flash-preview",
}


@dataclass(frozen=True)
class ModelMetadata:
    """Static metadata for a provider/model pair."""

    provider: str
    model: str
    context_window_tokens: int | None
    max_output_tokens_default: int | None
    input_cost_per_million_tokens: float | None
    output_cost_per_million_tokens: float | None
    source: str
    source_url: str
    verified_on: str


@dataclass(frozen=True)
class PRContext:
    """Identifies a pull request uniquely. Value object — no I/O ever belongs here."""

    owner: str
    repo: str
    pr_number: int
    head_sha: str = ""

    @property
    def log_label(self) -> str:
        """Short string for log messages, e.g. 'acme/api#42'."""
        return f"{self.owner}/{self.repo}#{self.pr_number}"

    def pr_url(self, cfg) -> str:
        """Construct the web URL for this PR given an SCM config."""
        base_url = cfg.url.rstrip("/")
        match cfg.provider:
            case "github":
                return f"{base_url}/{self.owner}/{self.repo}/pull/{self.pr_number}"
            case "gitlab":
                return f"{base_url}/{self.owner}/{self.repo}/-/merge_requests/{self.pr_number}"
            case "bitbucket":
                return f"https://bitbucket.org/{self.owner}/{self.repo}/pull-requests/{self.pr_number}"
            case "bitbucket_server":
                return (
                    f"{base_url}/projects/{self.owner}/repos/{self.repo}"
                    f"/pull-requests/{self.pr_number}"
                )
            case _:
                return f"{base_url}/{self.owner}/{self.repo}/pulls/{self.pr_number}"

    def idempotency_key(self, scm_cfg, llm_cfg, base_sha: str = "") -> str:
        """Stable run fingerprint.

        Same key means this exact PR/range/config was already reviewed.
        """
        import code_review as _pkg

        agent_version = getattr(_pkg, "__version__", "0.1.0")
        config_hash = hashlib.sha256(
            f"{scm_cfg.provider}:{scm_cfg.url}:{llm_cfg.provider}:{llm_cfg.model}".encode()
        ).hexdigest()[:16]
        return (
            f"{scm_cfg.provider}/{self.owner}/{self.repo}/pr/{self.pr_number}"
            f"/head/{(self.head_sha or '').strip()}/base/{(base_sha or '').strip()}"
            f"/agent/{agent_version}/config/{config_hash}"
        )


def _model_metadata_resource_text() -> str:
    """Load the packaged JSON seed for model metadata."""
    return resources.files("code_review").joinpath(_MODEL_METADATA_FILENAME).read_text(
        encoding="utf-8"
    )


@lru_cache(maxsize=1)
def _load_model_metadata_catalog() -> dict[tuple[str, str], ModelMetadata]:
    """Load and cache the local JSON-backed model metadata catalog."""
    raw_entries = json.loads(_model_metadata_resource_text())
    if not isinstance(raw_entries, list):
        raise ValueError(f"{_MODEL_METADATA_FILENAME} must contain a JSON array of model entries")

    catalog: dict[tuple[str, str], ModelMetadata] = {}
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{_MODEL_METADATA_FILENAME} entries must be JSON objects")

        metadata = ModelMetadata(
            provider=str(entry.get("provider", "")).strip().lower(),
            model=str(entry.get("model", "")).strip(),
            context_window_tokens=entry.get("context_window_tokens"),
            max_output_tokens_default=entry.get("max_output_tokens_default"),
            input_cost_per_million_tokens=entry.get("input_cost_per_million_tokens"),
            output_cost_per_million_tokens=entry.get("output_cost_per_million_tokens"),
            source=str(entry.get("source", "")).strip(),
            source_url=str(entry.get("source_url", "")).strip(),
            verified_on=str(entry.get("verified_on", "")).strip(),
        )
        if not metadata.provider or not metadata.model:
            raise ValueError(
                f"{_MODEL_METADATA_FILENAME} entries must include non-empty provider and model"
            )
        catalog[(metadata.provider, metadata.model)] = metadata
    return catalog


def get_model_metadata_catalog() -> dict[tuple[str, str], ModelMetadata]:
    """Return a shallow copy of the local model metadata catalog.

    This function acts as the seam for a future service-backed metadata source.
    """
    return dict(_load_model_metadata_catalog())


def get_model_metadata(
    provider: str | None = None,
    model: str | None = None,
) -> ModelMetadata | None:
    """Look up static metadata for a provider/model pair."""
    if provider is None or model is None:
        config = get_llm_config()
        provider = config.provider
        model = config.model
    provider_key = (provider or "").strip().lower()
    model_key = (model or "").strip()
    catalog = _load_model_metadata_catalog()
    metadata = catalog.get((provider_key, model_key))
    if metadata is not None:
        return metadata
    alias = _MODEL_ALIASES.get((provider_key, model_key))
    if alias is None:
        return None
    return catalog.get((provider_key, alias))


def get_model_token_costs(
    provider: str | None = None, model: str | None = None
) -> tuple[float | None, float | None]:
    """Return input/output token cost per million tokens for a provider/model pair."""
    metadata = get_model_metadata(provider, model)
    if metadata is None:
        return (None, None)
    return (
        metadata.input_cost_per_million_tokens,
        metadata.output_cost_per_million_tokens,
    )


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


def _secret_value(secret: SecretStr | str | None) -> str:
    if secret is None:
        return ""
    raw = secret.get_secret_value() if isinstance(secret, SecretStr) else str(secret)
    return raw.strip()


def _get_task_value(primary: Any, task: Any, field: str) -> Any:
    value = getattr(task, field, None)
    if value is not None:
        return value
    return getattr(primary, field)


def _task_config(primary: Any, task: Any) -> Any:
    """Return a small config-like object with task overrides resolved."""
    from types import SimpleNamespace

    provider = _get_task_value(primary, task, "provider")
    task_api_key = getattr(task, "api_key", None)
    if task_api_key is not None:
        api_key = task_api_key
    elif provider == getattr(primary, "provider"):
        api_key = getattr(primary, "api_key")
    else:
        api_key = None

    return SimpleNamespace(
        provider=provider,
        model=_get_task_value(primary, task, "model"),
        api_key=api_key,
    )


def _get_configured_model_from_config(config: Any) -> Any:
    """
    Return the configured LLM instance for ADK from a config-like object.

    When an API key is set, it is applied to the provider-specific env var so
    ADK/LiteLLM see it. Gemini/Vertex return model strings for ADK's native
    registry; other providers use ADK LiteLLM when available.
    """
    global _INJECTED_PROVIDER_API_ENV, _PREVIOUS_PROVIDER_API_VALUE

    env_var = _PROVIDER_API_KEY_ENV.get(config.provider)
    api_key = _secret_value(config.api_key)

    # Keep injected provider credentials scoped to the current config/provider call.
    if _INJECTED_PROVIDER_API_ENV and (_INJECTED_PROVIDER_API_ENV != env_var or not api_key):
        _clear_injected_provider_api_env()

    if env_var and api_key:
        if _INJECTED_PROVIDER_API_ENV != env_var:
            _PREVIOUS_PROVIDER_API_VALUE = os.environ.get(env_var)
        os.environ[env_var] = api_key
        _INJECTED_PROVIDER_API_ENV = env_var

    resolved_model = _MODEL_ALIASES.get((config.provider, config.model), config.model)

    if config.provider in {"gemini", "vertex"}:
        return resolved_model
    # Use LiteLLM for OpenAI, Anthropic, Ollama, OpenRouter
    if config.provider == "openai":
        litellm_model = f"openai/{resolved_model}"
    elif config.provider == "anthropic":
        litellm_model = f"anthropic/{resolved_model}"
    elif config.provider == "ollama":
        litellm_model = f"ollama_chat/{resolved_model}"
    elif config.provider == "openrouter":
        litellm_model = f"openrouter/{resolved_model}"
    else:
        litellm_model = resolved_model

    try:
        from google.adk.models.lite_llm import LiteLlm

        return LiteLlm(model=litellm_model)
    except ImportError:
        # Fallback if ADK LiteLLM not available
        return config.model


def get_configured_model() -> Any:
    """
    Return the primary configured review LLM instance for ADK.

    Reads LLM_PROVIDER, LLM_MODEL, and LLM_API_KEY from env/config.
    """
    return _get_configured_model_from_config(get_llm_config())


def get_configured_summary_model() -> Any:
    """Return the configured summary LLM, falling back to the primary LLM field by field."""
    return _get_configured_model_from_config(
        _task_config(get_llm_config(), get_summary_llm_config())
    )


def get_configured_verification_model() -> Any:
    """Return the configured verification LLM, falling back to the primary LLM field by field."""
    return _get_configured_model_from_config(
        _task_config(get_llm_config(), get_verification_llm_config())
    )


def get_context_window() -> int:
    """
    Return context window size in tokens for runner chunking.
    Explicit LLM_CONTEXT_WINDOW still wins. Otherwise use model metadata when available.
    """
    config = get_llm_config()
    if not os.getenv("LLM_CONTEXT_WINDOW", "").strip():
        metadata = get_model_metadata(config.provider, config.model)
        if metadata and metadata.context_window_tokens is not None:
            return metadata.context_window_tokens
    return config.context_window


def get_max_output_tokens() -> int:
    """Return max output tokens from config or model metadata."""
    config = get_llm_config()
    if not os.getenv("LLM_MAX_OUTPUT_TOKENS", "").strip():
        metadata = get_model_metadata(config.provider, config.model)
        if metadata and metadata.max_output_tokens_default is not None:
            return metadata.max_output_tokens_default
    return config.max_output_tokens


# Models that only support a fixed temperature (any other value raises UnsupportedParamsError).
# For these models the parameter should be omitted entirely; their fixed value is their default.
_FIXED_TEMPERATURE_PREFIXES: tuple[str, ...] = ("gpt-5",)


def get_effective_temperature_for_model(provider: str, model: str, requested: float) -> float | None:
    """Return the temperature to pass for a provider/model pair, or None to omit it.

    Some models (e.g. the gpt-5 family) only support a fixed temperature value
    that is also their default. Passing any other value raises
    litellm.UnsupportedParamsError at runtime, and passing the fixed value
    explicitly is redundant. This helper returns None for those models so
    callers can skip the parameter entirely.
    """
    provider_key = (provider or "").strip().lower()
    model_key = (model or "").strip()
    resolved = _MODEL_ALIASES.get((provider_key, model_key), model_key)

    if any(resolved.startswith(p) for p in _FIXED_TEMPERATURE_PREFIXES):
        return None
    return requested


def get_effective_temperature(requested: float) -> float | None:
    """Return the temperature to pass for the primary configured model."""
    config = get_llm_config()
    return get_effective_temperature_for_model(config.provider, config.model, requested)
