"""LLM pass: condense fetched context into a short review brief."""

from __future__ import annotations

import logging

import litellm

from code_review.config import get_llm_config
from code_review.models import get_configured_model

logger = logging.getLogger(__name__)


def distill_context_text(
    raw_context: str,
    *,
    max_output_tokens: int,
) -> str:
    """Summarize requirements-focused context for the review agent."""
    if not raw_context.strip():
        return ""
    llm = get_llm_config()
    model = get_configured_model()
    system = (
        "You distill linked issue/ticket/spec text into a concise brief for a code reviewer. "
        "Extract requirements, acceptance criteria, and explicit constraints. "
        "Omit boilerplate and noise. Use bullet points where helpful. "
        "Do not invent requirements not present in the source."
    )
    user = f"Source material:\n\n{raw_context}\n\nProduce the brief."
    try:
        resp = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_output_tokens,
            temperature=llm.temperature,
        )
    except Exception as e:
        logger.warning("Context distillation LLM call failed: %s", e)
        return raw_context[:8000] + ("\n…" if len(raw_context) > 8000 else "")
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return raw_context[:8000] + ("\n…" if len(raw_context) > 8000 else "")
