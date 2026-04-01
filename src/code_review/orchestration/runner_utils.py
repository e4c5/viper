"""Async agent runner utilities and structured-findings helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import code_review
from google.genai import types

if TYPE_CHECKING:
    from google.adk.agents.callback_context import ReadonlyContext

from code_review import observability
from code_review.json_utils import iter_json_candidates
from code_review.models import PRContext
from code_review.providers.base import RateLimitError
from code_review.schemas.findings import FindingsBatchV1, FindingV1

import json

logger = logging.getLogger(__name__)

APP_NAME = "code_review"
USER_ID = "reviewer"
AGENT_VERSION = getattr(code_review, "__version__", "0.1.0")


# ---------------------------------------------------------------------------
# SSL teardown suppressor
# ---------------------------------------------------------------------------

def _suppress_ssl_teardown_errors(loop, context: dict) -> None:
    """Asyncio exception handler that silences known SSL-transport teardown noise."""
    exc = context.get("exception")
    msg = context.get("message", "")
    _teardown_msg = "SSL" in msg or "Fatal write error" in msg or "write backlog" in msg
    _teardown_exc = (isinstance(exc, OSError) and getattr(exc, "errno", None) == 9) or (
        isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc)
    )
    if _teardown_msg and _teardown_exc:
        return
    loop.default_exception_handler(context)


# ---------------------------------------------------------------------------
# PartialResponseCollectionError
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PartialResponseCollectionError(Exception):
    """Raised when a workflow emits some final responses before failing."""

    responses: list[tuple[str, str]]
    cause: Exception


# ---------------------------------------------------------------------------
# ADK templating bypass
# ---------------------------------------------------------------------------

def _bypass_adk_templating(agent: Any) -> None:
    """Recursively wrap agent instructions in a provider that bypasses ADK templating."""
    sub_agents = getattr(agent, "sub_agents", [])
    for sa in sub_agents:
        _bypass_adk_templating(sa)

    instruction = getattr(agent, "instruction", None)
    if isinstance(instruction, str):
        agent.instruction = lambda _: instruction

    global_instruction = getattr(agent, "global_instruction", None)
    if isinstance(global_instruction, str):
        agent.global_instruction = lambda _: global_instruction


# ---------------------------------------------------------------------------
# Single-response async runner
# ---------------------------------------------------------------------------

async def _collect_response_async(runner, session_id: str, content: types.Content) -> str:
    """Run agent once via run_async and return concatenated final response text."""
    asyncio.get_running_loop().set_exception_handler(_suppress_ssl_teardown_errors)
    _bypass_adk_templating(runner.agent)

    parts: list[str] = []
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    parts.append(part.text)
    text = "\n".join(parts)
    if os.getenv("CODE_REVIEW_PRINT_RAW_RESPONSE", "").strip() in ("1", "true", "TRUE"):
        print(f"RAW LLM RESPONSE (session={session_id}):\n{text}")
    return text


def _run_agent_and_collect_response(
    runner, session_id: str, content: types.Content
) -> str:
    """Run agent once and return concatenated final response text (uses async API)."""
    return asyncio.run(_collect_response_async(runner, session_id, content))


# ---------------------------------------------------------------------------
# Multi-response async runner (batch workflow)
# ---------------------------------------------------------------------------


def _describe_event_parts(event) -> list[str]:
    """Return a compact description of the event part types for debug logging."""
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    part_types: list[str] = []
    for part in parts:
        if getattr(part, "text", None):
            part_types.append("text")
            continue
        if getattr(part, "function_call", None):
            fc = part.function_call
            part_types.append(f"fn_call:{getattr(fc, 'name', '?')}")
            continue
        if getattr(part, "function_response", None):
            fr = part.function_response
            part_types.append(f"fn_resp:{getattr(fr, 'name', '?')}")
            continue
        part_types.append("other")
    return part_types


def _log_batch_event(event_count: int, event) -> None:
    """Emit per-event debug logging for batch workflow runs."""
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    logger.debug(
        "[batch] event #%d author=%r is_final=%s has_content=%s parts=%s",
        event_count,
        getattr(event, "author", "<unknown>"),
        event.is_final_response(),
        bool(parts),
        _describe_event_parts(event) or "[]",
    )


def _collect_text_parts(event) -> list[str]:
    """Return all text parts from an event, preserving order."""
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    return [part.text for part in parts if getattr(part, "text", None)]


def _append_final_response_text(event, responses: list[tuple[str, str]]) -> None:
    """Collect one event's final response text when present."""
    if not event.is_final_response():
        return
    texts = _collect_text_parts(event)
    if not texts:
        return
    author = getattr(event, "author", "<unknown>")
    logger.debug(
        "[batch] collected final response from author=%r text_len=%d",
        author,
        sum(len(text) for text in texts),
    )
    responses.append((author, "\n".join(texts)))


def _should_wrap_partial_response_error(exc: Exception, event_count: int) -> bool:
    """Return True when the caller should preserve partial responses with the error."""
    return isinstance(exc, RateLimitError) or event_count > 0


def _raise_collection_error(
    exc: Exception,
    *,
    responses: list[tuple[str, str]],
    event_count: int,
) -> None:
    """Raise the appropriate error type for a failed batch workflow collection."""
    logger.debug(
        "[batch] _collect_final_response_texts_async raised after %d event(s): %s",
        event_count,
        exc,
    )
    if _should_wrap_partial_response_error(exc, event_count):
        raise PartialResponseCollectionError(responses=responses, cause=exc) from exc
    raise exc

async def _collect_final_response_texts_async(
    runner, session_id: str, content: types.Content
) -> list[tuple[str, str]]:
    """Run agent once and collect text-bearing final responses per participating agent."""
    asyncio.get_running_loop().set_exception_handler(_suppress_ssl_teardown_errors)

    logger.debug(
        "[batch] _collect_final_response_texts_async starting session=%s",
        session_id,
    )
    _bypass_adk_templating(runner.agent)

    responses: list[tuple[str, str]] = []
    event_count = 0
    try:
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=session_id,
            new_message=content,
        ):
            event_count += 1
            _log_batch_event(event_count, event)
            _append_final_response_text(event, responses)
    except Exception as exc:
        _raise_collection_error(exc, responses=responses, event_count=event_count)
    logger.debug(
        "[batch] _collect_final_response_texts_async done: %d event(s) received, "
        "%d final response(s) collected session=%s",
        event_count,
        len(responses),
        session_id,
    )
    return responses


def _run_agent_and_collect_responses(
    runner, session_id: str, content: types.Content
) -> list[tuple[str, str]]:
    """Run agent once and return text-bearing final responses from all participating agents."""
    return asyncio.run(
        _collect_final_response_texts_async(runner, session_id, content)
    )


# ---------------------------------------------------------------------------
# Structured findings parsing
# ---------------------------------------------------------------------------

def _parse_findings_json(text: str) -> object:
    """Parse a structured findings object from raw text or a fenced JSON block."""
    last_error: json.JSONDecodeError | None = None
    for raw in iter_json_candidates(text):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
    snippet = text.strip()
    if len(snippet) > 300:
        snippet = snippet[:300] + "..."
    if last_error is not None:
        raise ValueError(
            "Failed to parse structured findings JSON from agent response. "
            f"Last JSON error: {last_error}. Response snippet: {snippet!r}"
        ) from last_error
    raise ValueError(
        "Failed to parse structured findings JSON from agent response: "
        f"no JSON candidate found. Response snippet: {snippet!r}"
    )


def _findings_from_response(response_text: str) -> list[FindingV1]:
    """Parse response text into validated findings."""
    raw = _parse_findings_json(response_text)
    if not isinstance(raw, dict):
        return []
    try:
        return FindingsBatchV1.model_validate(raw).findings
    except Exception as e:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Failed to parse structured findings response: %r (error: %s)",
                raw,
                e,
                exc_info=True,
            )
        return []


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

def _log_run_complete(
    trace_id: str,
    owner: str,
    repo: str,
    pr_number: int,
    files_count: int,
    findings_count: int,
    posts_count: int,
    duration_ms: float,
) -> None:
    """Emit structured run_complete log."""
    logger.info(
        "run_complete",
        extra={
            "trace_id": trace_id,
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "files_count": files_count,
            "findings_count": findings_count,
            "posts_count": posts_count,
            "duration_ms": round(duration_ms, 2),
        },
    )


@dataclass
class ReviewRunObservability:
    """Owns run teardown timing and emits the paired log + observability finish calls."""

    trace_id: str
    run_handle: Any
    log_run_complete: Any | None = None
    finish_run: Any | None = None
    start_time: float = field(default_factory=time.perf_counter)

    @staticmethod
    def _count(value: Any) -> int:
        if isinstance(value, int):
            return value
        return len(value)

    def finish(
        self,
        pr_ctx: PRContext,
        paths,
        findings,
        posts,
        *,
        context_brief_attached: bool = False,
    ) -> None:
        duration_ms = (time.perf_counter() - self.start_time) * 1000
        files_count = self._count(paths)
        findings_count = self._count(findings)
        posts_count = self._count(posts)
        log_run_complete = self.log_run_complete or _log_run_complete
        finish_run = self.finish_run or observability.finish_run
        log_run_complete(
            self.trace_id,
            pr_ctx.owner,
            pr_ctx.repo,
            pr_ctx.pr_number,
            files_count=files_count,
            findings_count=findings_count,
            posts_count=posts_count,
            duration_ms=duration_ms,
        )
        finish_run(
            self.run_handle,
            pr_ctx.owner,
            pr_ctx.repo,
            pr_ctx.pr_number,
            files_count=files_count,
            findings_count=findings_count,
            posts_count=posts_count,
            duration_seconds=duration_ms / 1000.0,
            context_brief_attached=context_brief_attached,
        )


# ---------------------------------------------------------------------------
# Reply-dismissal LLM runner (lives here because it instantiates a Runner)
# ---------------------------------------------------------------------------

def _run_reply_dismissal_llm(user_message: str) -> str:
    """Run the tool-free reply-dismissal agent once; return raw model text."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    from code_review.agent.reply_dismissal_agent import create_reply_dismissal_agent

    agent = create_reply_dismissal_agent()
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
        auto_create_session=True,
    )
    session_id = f"reply-dismissal/{uuid.uuid4().hex[:12]}"
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "LLM request (reply-dismissal) session=%s prompt=%s",
            session_id,
            user_message,
        )
    content = types.Content(role="user", parts=[types.Part(text=user_message)])
    return _run_agent_and_collect_response(runner, session_id, content)
