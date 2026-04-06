"""Model factory and model metadata helpers."""

import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from code_review.config import get_llm_config


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
                return f"{base_url}/projects/{self.owner}/repos/{self.repo}/pull-requests/{self.pr_number}"
            case _:
                return f"{base_url}/{self.owner}/{self.repo}/pulls/{self.pr_number}"

    def idempotency_key(self, scm_cfg, llm_cfg, base_sha: str = "") -> str:
        """Stable run fingerprint. Same key means this exact PR/range/config was already reviewed."""
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


def get_configured_model() -> Any:
    """
    Return the configured LLM instance for ADK.
    Reads LLM_PROVIDER, LLM_MODEL, and LLM_API_KEY from env/config.
    Uses LiteLLM for all models in order to pass the config api_key via constructor.
    """
    config = get_llm_config()
    api_key = config.api_key.get_secret_value().strip() if config.api_key is not None else None

    resolved_model = _MODEL_ALIASES.get((config.provider, config.model), config.model)

    # Prefix with provider so LiteLLM knows how to route the request
    if config.provider == "gemini":
        litellm_model = f"gemini/{resolved_model}"
    elif config.provider == "vertex":
        litellm_model = f"vertex_ai/{resolved_model}"
    elif config.provider == "openai":
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
        # Suppress the warning emitted when passing a Gemini model via LiteLLM
        os.environ["ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS"] = "true"
        from google.adk.models.lite_llm import LiteLlm

        kwargs = {"model": litellm_model}
        if api_key:
            kwargs["api_key"] = api_key

        return LiteLlm(**kwargs)
    except ImportError:
        # Fallback if ADK LiteLLM not available
        return config.model


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
