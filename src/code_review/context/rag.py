"""Over-budget path: semantic query from diff, chunking, embeddings, vector search."""

from __future__ import annotations

import logging
import re

from code_review.config import get_llm_config
from code_review.context.distiller import _litellm_model_name
from code_review.llm_telemetry import log_llm_usage, usage_from_litellm_response
from code_review.models import get_configured_model, get_effective_temperature

logger = logging.getLogger(__name__)


def _choice_message_text(choice: object) -> str:
    if isinstance(choice, dict):
        msg = choice.get("message")
    else:
        msg = getattr(choice, "message", None)
    if isinstance(msg, dict):
        content = msg.get("content")
    else:
        content = getattr(msg, "content", None)
    if isinstance(content, list):
        content = " ".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return content.strip() if isinstance(content, str) and content.strip() else ""


def _heuristic_query_from_diff(snippet: str) -> str:
    paths = set(re.findall(r"^[+-]{3} [ab]/(.+)$", snippet, re.MULTILINE))
    hint = ", ".join(sorted(paths)[:8])
    return f"Code changes in: {hint}" if hint else "pull request code changes"


def _llm_api_key(llm: object) -> str | None:
    """Return the configured provider API key as a plain string, or None."""
    key = getattr(llm, "api_key", None)
    if key is None:
        return None
    raw = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
    return raw.strip() or None


# Known litellm embedding provider prefixes (``provider/model`` form). Prefixes
# not in this map are treated as part of a bare model name (e.g. HuggingFace
# ids like ``BAAI/bge-m3``), which litellm resolves to OpenAI embeddings.
_EMBEDDING_PROVIDER_ALIASES: dict[str, str] = {
    "openai": "openai",
    "azure": "azure",
    "gemini": "gemini",
    "vertex": "vertex",
    "vertex_ai": "vertex",
    "anthropic": "anthropic",
    "ollama": "ollama",
    "openrouter": "openrouter",
    "deepseek": "deepseek",
    "bedrock": "bedrock",
    "cohere": "cohere",
    "huggingface": "huggingface",
    "mistral": "mistral",
    "voyage": "voyage",
}


def _embedding_provider_matches_llm(model: str, llm_provider: str) -> bool:
    """Return True when the embedding model targets the configured LLM provider.

    ``provider/model`` prefixes are honored; bare model names (e.g.
    ``text-embedding-3-small``) default to OpenAI embeddings in litellm.
    """
    provider = (llm_provider or "").strip().lower()
    prefix, sep, _ = (model or "").partition("/")
    emb_provider = _EMBEDDING_PROVIDER_ALIASES.get(prefix.strip().lower())
    if not sep or emb_provider is None:
        return provider == "openai"
    return emb_provider == provider


def build_semantic_query_from_diff(diff_text: str, max_diff_chars: int = 14_000) -> str:
    """Lightweight LLM pass: intent + entities for similarity search."""
    snippet = (diff_text or "")[:max_diff_chars]
    if not snippet.strip():
        return "pull request code changes"
    llm = get_llm_config()
    model = _litellm_model_name(get_configured_model(), llm.model)
    system = (
        "You write a single short paragraph (max 120 words) describing what the pull request "
        "changes and why, including key file paths and symbols if obvious. "
        "Output plain text only, no markdown."
    )
    user = f"Unified diff (truncated):\n\n{snippet}"
    _temperature = get_effective_temperature(llm.temperature)
    try:
        import litellm

        resp = litellm.completion(
            model=model,
            api_key=_llm_api_key(llm),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=256,
            **({"temperature": _temperature} if _temperature is not None else {}),
        )
        log_llm_usage(
            logger,
            task="semantic_query",
            provider=llm.provider,
            model=model,
            usage=usage_from_litellm_response(resp),
            response_text_len=None,
        )
        choices = (
            resp["choices"] if isinstance(resp, dict) else getattr(resp, "choices", None)
        ) or []
        if choices:
            text = _choice_message_text(choices[0])
            if text:
                return text
    except Exception as e:
        logger.warning("Semantic query LLM pass failed, falling back to heuristics: %s", e)
    return _heuristic_query_from_diff(snippet)


def chunk_plain_text(text: str, max_chunk_chars: int = 1800, overlap: int = 200) -> list[str]:
    """Split long text into overlapping segments for embedding."""
    if max_chunk_chars <= 0:
        raise ValueError(f"max_chunk_chars must be positive, got {max_chunk_chars}")
    if overlap < 0:
        raise ValueError(f"overlap must be non-negative, got {overlap}")
    if overlap >= max_chunk_chars:
        raise ValueError(
            f"overlap ({overlap}) must be less than max_chunk_chars ({max_chunk_chars})"
        )
    text = text.strip()
    if len(text) <= max_chunk_chars:
        return [text] if text else []
    parts: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chunk_chars, n)
        chunk = text[start:end].strip()
        if chunk:
            parts.append(chunk)
        if end >= n:
            break
        start = max(0, end - overlap)
    return parts


def embed_texts(texts: list[str], model: str) -> list[list[float]]:
    """Return embeddings in API order; skips empty inputs."""
    if not texts:
        return []
    try:
        import litellm

        # Only forward the primary LLM key when the embedding model targets the
        # same provider — otherwise litellm must resolve the embedding provider's
        # own env credentials (and we must not leak the key to another service).
        llm = get_llm_config()
        kwargs: dict = {}
        if _embedding_provider_matches_llm(model, llm.provider):
            api_key = _llm_api_key(llm)
            if api_key:
                kwargs["api_key"] = api_key
        resp = litellm.embedding(model=model, input=texts, **kwargs)
    except Exception as e:
        logger.warning("Embedding call failed (%s): %s", model, e)
        raise
    data = resp.get("data") or []
    out: list[list[float]] = []
    for item in sorted(data, key=lambda x: x.get("index", 0)):
        emb = item.get("embedding")
        if isinstance(emb, list):
            out.append([float(x) for x in emb])
    if len(out) != len(texts):
        raise RuntimeError("embedding count mismatch")
    return out


def embed_query_text(query: str, embedding_model: str) -> list[float]:
    vecs = embed_texts([query], embedding_model)
    return vecs[0] if vecs else []
