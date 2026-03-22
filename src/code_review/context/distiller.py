"""LLM pass: condense fetched context into a short review brief."""

from __future__ import annotations

import logging

import litellm

from code_review.config import get_llm_config
from code_review.models import get_configured_model

logger = logging.getLogger(__name__)
_FALLBACK_LIMIT = 8000


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
        text = _text_from_content_block(block)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _text_from_content_block(block: object) -> str:
    if isinstance(block, str):
        return block.strip()
    if not isinstance(block, dict):
        return ""
    for key in ("text", "output_text", "content"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _raw_context_fallback(raw_context: str) -> str:
    return raw_context[:_FALLBACK_LIMIT] + ("\n…" if len(raw_context) > _FALLBACK_LIMIT else "")


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
        return _raw_context_fallback(raw_context)
    choices = (resp["choices"] if isinstance(resp, dict) else getattr(resp, "choices", None)) or []
    if not choices:
        return _raw_context_fallback(raw_context)
    msg = (
        choices[0]["message"]
        if isinstance(choices[0], dict)
        else getattr(choices[0], "message", None)
    )
    content = msg["content"] if isinstance(msg, dict) else getattr(msg, "content", None)
    distilled = _distilled_text_from_content(content)
    if distilled:
        return distilled
    return _raw_context_fallback(raw_context)
