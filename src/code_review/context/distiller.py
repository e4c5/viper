"""LLM pass: condense fetched context into a short review brief."""

from __future__ import annotations

import logging
import uuid

from code_review.config import get_llm_config
from code_review.llm_telemetry import log_adk_llm_usage
from code_review.models import get_configured_model, get_effective_temperature

logger = logging.getLogger(__name__)
_FALLBACK_LIMIT = 8000
_DISTILL_INSTRUCTION = (
    "You distill linked issue/ticket/spec text into a concise brief for a code reviewer. "
    "Extract requirements, acceptance criteria, and explicit constraints. "
    "Omit boilerplate and noise. Use bullet points where helpful. "
    "Do not invent requirements not present in the source. "
    "Output only the brief."
)


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


def _create_context_distillation_agent(max_output_tokens: int):
    """Create the context distillation agent using the primary LLM configuration."""
    from google.adk.agents import Agent
    from google.genai import types

    llm = get_llm_config()
    _temperature = get_effective_temperature(llm.temperature)
    generate_content_config = types.GenerateContentConfig(
        **({"temperature": _temperature} if _temperature is not None else {}),
        max_output_tokens=max_output_tokens,
    )
    return Agent(
        model=get_configured_model(),
        name="context_distillation_agent",
        instruction=_DISTILL_INSTRUCTION,
        tools=[],
        generate_content_config=generate_content_config,
        after_model_callback=lambda callback_context, llm_response: log_adk_llm_usage(
            logger,
            task="context_distillation",
            response=llm_response,
            provider=llm.provider,
            model=llm.model,
        ),
    )


def _run_context_distillation_agent(user_message: str, max_output_tokens: int) -> str:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from code_review.orchestration.runner_utils import APP_NAME, _run_agent_and_collect_response

    agent = _create_context_distillation_agent(max_output_tokens)
    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    session_id = f"context-distillation/{uuid.uuid4().hex[:12]}"
    logger.debug(
        "LLM request (context distillation) session=%s prompt=%s",
        session_id,
        user_message,
    )
    content = types.Content(role="user", parts=[types.Part(text=user_message)])
    return _run_agent_and_collect_response(runner, session_id, content)


def distill_context_text(
    raw_context: str,
    *,
    max_output_tokens: int,
) -> str:
    """Summarize requirements-focused context for the review agent."""
    if not raw_context.strip():
        return ""
    user = f"Source material:\n\n{raw_context}\n\nProduce the brief."
    try:
        distilled = _run_context_distillation_agent(user, max_output_tokens).strip()
    except Exception as e:
        logger.warning("Context distillation LLM call failed: %s", e)
        return _raw_context_fallback(raw_context)
    if distilled:
        return distilled
    logger.warning("Context distillation LLM call returned empty content; using raw fallback")
    return _raw_context_fallback(raw_context)
