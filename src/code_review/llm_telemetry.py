"""Consistent LLM request/usage telemetry helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from code_review.config import get_llm_config, get_summary_llm_config, get_verification_llm_config
from code_review.logging_config import emit_package_log

# Everything else is a review agent, which ADK names per diff batch.
_NON_REVIEW_TASKS = frozenset(
    {"context_distillation", "reply_dismissal", "summary", "semantic_query", "verification"}
)


@dataclass
class ReviewUsage:
    """Token totals of the review-agent calls in one run, across all diff batches.

    Support calls (verification, summary, ...) are excluded: they do not use the
    model under comparison.
    """

    provider: str | None = None
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    thoughts_tokens: int = 0


_active_review_usage: ContextVar[ReviewUsage | None] = ContextVar(
    "code_review_active_review_usage", default=None
)


@contextmanager
def collect_review_usage(
    provider: str | None = None, model: str | None = None
) -> Iterator[ReviewUsage]:
    """Total review-agent usage for the calls made inside this block.

    *provider* and *model* also label the review agents' ``llm_usage`` log lines,
    which otherwise name the process-wide model even for a per-run override.
    """
    usage = ReviewUsage(provider=provider, model=model)
    token = _active_review_usage.set(usage)
    try:
        yield usage
    finally:
        _active_review_usage.reset(token)


def _as_token_count(value: object) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _get_value(obj: object, *names: str) -> object:
    current = obj
    for name in names:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(name)
        else:
            current = getattr(current, name, None)
    return current


def effective_llm_identity(task: str) -> tuple[str, str]:
    """Return the provider/model that a named LLM task should use."""
    primary = get_llm_config()
    if task == "summary":
        override = get_summary_llm_config()
    elif task == "verification":
        override = get_verification_llm_config()
    else:
        override = None
    provider = getattr(override, "provider", None) or primary.provider
    model = getattr(override, "model", None) or primary.model
    return (provider, model)


def response_text_length(response: object) -> int:
    """Return total text length from an ADK response-like object."""
    parts = getattr(getattr(response, "content", None), "parts", None) or ()
    return sum(len(getattr(part, "text", "") or "") for part in parts)


def _cache_status(cached_tokens: object) -> str:
    if cached_tokens is None:
        return "unreported"
    try:
        return "hit" if int(cached_tokens) > 0 else "miss"
    except (TypeError, ValueError):
        return "unreported"


def log_llm_usage(
    logger: logging.Logger,
    *,
    task: str,
    provider: str,
    model: str,
    usage: object | None,
    response_text_len: int | None = None,
    finish_reason: object | None = None,
    interrupted: object | None = None,
    turn_complete: object | None = None,
) -> None:
    """Emit one normalized LLM usage log line."""
    prompt_tokens = _get_value(usage, "prompt_token_count")
    completion_tokens = _get_value(usage, "candidates_token_count")
    total_tokens = _get_value(usage, "total_token_count")
    cached_tokens = _get_value(usage, "cached_content_token_count")
    tool_prompt_tokens = _get_value(usage, "tool_use_prompt_token_count")
    thoughts_tokens = _get_value(usage, "thoughts_token_count")
    cache_status = _cache_status(cached_tokens)

    if prompt_tokens is None:
        prompt_tokens = _get_value(usage, "prompt_tokens")
    if completion_tokens is None:
        completion_tokens = _get_value(usage, "completion_tokens")
    if total_tokens is None:
        total_tokens = _get_value(usage, "total_tokens")

    collector = _active_review_usage.get()
    if collector is not None and task not in _NON_REVIEW_TASKS:
        collector.prompt_tokens += _as_token_count(prompt_tokens)
        collector.completion_tokens += _as_token_count(completion_tokens)
        collector.cached_tokens += _as_token_count(cached_tokens)
        collector.thoughts_tokens += _as_token_count(thoughts_tokens)

    emit_package_log(
        logger,
        logging.INFO,
        (
            "llm_usage task=%s provider=%s model=%s prompt_tokens=%s "
            "completion_tokens=%s total_tokens=%s cached_tokens=%s "
            "cache_status=%s tool_prompt_tokens=%s thoughts_tokens=%s "
            "finish_reason=%s interrupted=%s turn_complete=%s response_text_len=%s"
        ),
        task,
        provider,
        model,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        cached_tokens,
        cache_status,
        tool_prompt_tokens,
        thoughts_tokens,
        finish_reason,
        interrupted,
        turn_complete,
        response_text_len,
    )


def log_adk_llm_usage(
    logger: logging.Logger,
    *,
    task: str,
    response: object,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    """Log usage from an ADK model response."""
    eff_provider, eff_model = effective_llm_identity(task)
    collector = _active_review_usage.get()
    if collector is not None and task not in _NON_REVIEW_TASKS:
        eff_provider = collector.provider or eff_provider
        eff_model = collector.model or eff_model
    resolved_provider = provider if provider is not None else eff_provider
    resolved_model = model if model is not None else eff_model
    log_llm_usage(
        logger,
        task=task,
        provider=resolved_provider,
        model=resolved_model,
        usage=getattr(response, "usage_metadata", None),
        response_text_len=response_text_length(response),
        finish_reason=getattr(response, "finish_reason", None),
        interrupted=getattr(response, "interrupted", None),
        turn_complete=getattr(response, "turn_complete", None),
    )


def usage_from_litellm_response(response: object) -> object | None:
    """Return the usage payload from a LiteLLM response-like object."""
    return _get_value(response, "usage")
