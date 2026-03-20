"""Over-budget path: semantic query from diff, chunking, embeddings, vector search."""

from __future__ import annotations

import logging
import re

import litellm

from code_review.config import get_llm_config
from code_review.models import get_configured_model

logger = logging.getLogger(__name__)


def build_semantic_query_from_diff(diff_text: str, max_diff_chars: int = 14_000) -> str:
    """Lightweight LLM pass: intent + entities for similarity search."""
    snippet = (diff_text or "")[:max_diff_chars]
    if not snippet.strip():
        return "pull request code changes"
    llm = get_llm_config()
    model = get_configured_model()
    system = (
        "You write a single short paragraph (max 120 words) describing what the pull request "
        "changes and why, including key file paths and symbols if obvious. "
        "Output plain text only, no markdown."
    )
    user = f"Unified diff (truncated):\n\n{snippet}"
    try:
        resp = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=256,
            temperature=llm.temperature,
        )
        choices = getattr(resp, "choices", None) or []
        if choices:
            text = getattr(getattr(choices[0], "message", None), "content", None)
            if isinstance(text, str) and text.strip():
                return text.strip()
    except Exception as e:
        logger.warning("Semantic query LLM pass failed, falling back to heuristics: %s", e)
    paths = set(re.findall(r"^[+-]{3} [ab]/(.+)$", snippet, re.MULTILINE))
    hint = ", ".join(sorted(paths)[:8])
    return f"Code changes in: {hint}" if hint else "pull request code changes"


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
        resp = litellm.embedding(model=model, input=texts)
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
