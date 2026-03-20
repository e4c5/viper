"""LLM pass: condense fetched context into a short review brief."""

from __future__ import annotations

import logging

import litellm

from code_review.config import get_llm_config
from code_review.models import get_configured_model

logger = logging.getLogger(__name__)


def _litellm_model_name(configured_model, fallback_model: str) -> str:
    """
    Normalize configured model to a string model id for litellm.completion().

    get_configured_model() may return:
    - str (Gemini/Vertex or fallback)
    - ADK LiteLlm object with `.model` attribute (OpenAI/Anthropic/Ollama/OpenRouter)
    """
    if isinstance(configured_model, str) and configured_model.strip():
        return configured_model
    model_attr = getattr(configured_model, "model", "")
    if isinstance(model_attr, str) and model_attr.strip():
        return model_attr
    return fallback_model


def _distilled_text_from_content(content: object) -> str:
    """Extract distilled text from LiteLLM message content variants."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str) and block.strip():
            parts.append(block.strip())
            continue
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
            continue
        # Some providers nest text under content[...].text or output_text keys.
        for key in ("output_text", "content"):
            nested = block.get(key)
            if isinstance(nested, str) and nested.strip():
                parts.append(nested.strip())
                break
    return "\n".join(parts).strip()


def distill_context_text(
    raw_context: str,
    *,
    max_output_tokens: int,
) -> str:
    """Summarize requirements-focused context for the review agent."""
    if not raw_context.strip():
        return ""
    llm = get_llm_config()
    model = _litellm_model_name(get_configured_model(), llm.model)
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
    distilled = _distilled_text_from_content(content)
    if distilled:
        return distilled
    return raw_context[:8000] + ("\n…" if len(raw_context) > 8000 else "")
