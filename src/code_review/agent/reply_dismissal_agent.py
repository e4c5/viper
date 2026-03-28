"""Tool-free ADK agent: classify a human reply on a review thread (Phase E.2)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from code_review.config import get_llm_config
from code_review.models import get_configured_model
from code_review.schemas.reply_dismissal import ReplyDismissalVerdictV1

if TYPE_CHECKING:
    from google.adk.agents import Agent

logger = logging.getLogger(__name__)

REPLY_DISMISSAL_INSTRUCTION = """\
You classify one pull-request review thread.

Your job: decide if the triggering human reply adequately addresses the concern raised in the
original automated review comment.

The thread may contain more than two comments. The user message will identify:
- the original automated review comment
- the original automated review comment severity
- the triggering human reply

Base your verdict on whether the triggering reply, together with any later clarifications in the
same thread, resolves the original concern.

If relevant PR diff context is provided, use it to ground your judgment. Prefer repository-specific
evidence from that diff over generic assumptions.

Do not rely on promised future work. Replies such as "I'll fix it", "will do", "I'll push a
change", "agree, will update", or similar intent-only acknowledgements do NOT resolve the concern
by themselves. Treat those as still unresolved unless the thread or diff already shows the change,
or the reply contains a concrete present-tense justification that fully addresses the original
concern.

Severity should influence how much justification you require before agreeing:
- nit: be pragmatic and easy to satisfy; reasonable developer tradeoffs or deferral are often enough
- low: require a plausible explanation or low-cost mitigation, but remain flexible
- medium: be hesitant to agree unless the reply provides a concrete, credible justification or fix
- high: require strong, specific evidence that the concern is invalid, mitigated, or already fixed

Severity is guidance, not an automatic verdict. If the thread or diff clearly resolves the concern,
you may still agree even for medium/high findings. If the reply is weak or dismissive, you should be
especially reluctant to agree for medium/high findings.

Output rules (critical):
- Respond with a single JSON object only. Do not include markdown fences.
- Schema:
  - "verdict": "agreed" OR "disagreed"
  - "reply_text": string — required when verdict is "disagreed": a short, professional reply \
to post on the thread explaining why the concern still stands. Use empty string when verdict \
is "agreed".

Use "agreed" when the human fix, explanation, or tradeoff reasonably resolves the review \
comment. Use "disagreed" when the thread still needs action or the reply misses the point.

When verdict is "disagreed" because the author only agreed to act later, thank them briefly but ask
for evidence in the PR, for example: "Thanks for agreeing. Please push the code changes so I can
re-check this thread."

Stay pragmatic and concise. Do not re-review the entire patch — only this thread."""


def create_reply_dismissal_agent() -> Agent:
    """Build a tool-free LlmAgent; caller embeds thread context in the user message."""
    from google.adk.agents import Agent
    from google.genai import types

    llm_cfg = get_llm_config()
    generate_content_config = types.GenerateContentConfig(
        temperature=llm_cfg.temperature,
        max_output_tokens=llm_cfg.max_output_tokens,
    )
    return Agent(
        model=get_configured_model(),
        name="reply_dismissal_agent",
        instruction=REPLY_DISMISSAL_INSTRUCTION,
        tools=[],
        generate_content_config=generate_content_config,
    )


def reply_dismissal_verdict_from_llm_text(text: str) -> ReplyDismissalVerdictV1 | None:
    """Parse final LLM text into a validated verdict, or None if parsing/validation fails."""
    for raw in _iter_reply_dismissal_json_candidates(text):
        try:
            return ReplyDismissalVerdictV1.model_validate(raw)
        except Exception as e:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Invalid reply-dismissal payload: %r (%s)", raw, e, exc_info=True)
    return None


def _first_markdown_fence_body(s: str) -> str | None:
    """Return inner text of the first fenced code block (linear scan; avoids ReDoS from regex)."""
    fence = "```"
    start_fence = s.find(fence)
    if start_fence == -1:
        return None
    i = start_fence + len(fence)
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    if i + 4 <= n and s[i : i + 4].lower() == "json":
        j = i + 4
        if j == n or s[j].isspace() or s[j] == "{":
            i = j
            while i < n and s[i].isspace():
                i += 1
    end_fence = s.find(fence, i)
    if end_fence == -1:
        return None
    return s[i:end_fence]


def _iter_reply_dismissal_json_candidates(text: str):
    """Yield dict candidates: whole-chunk parses first, then every ``{...}`` via raw_decode."""
    s = text.strip()
    chunks: list[str] = []
    fenced = _first_markdown_fence_body(s)
    if fenced is not None:
        chunks.append(fenced.strip())
    chunks.append(s)
    for chunk in chunks:
        yield from _iter_reply_dismissal_chunk_candidates(chunk)
    yield from _iter_json_objects_via_raw_decode(s)


def _iter_reply_dismissal_chunk_candidates(chunk: str):
    """Yield parsed objects from one chunk, including minimal repair for common LLM escapes."""
    for candidate in (chunk, _repair_common_llm_json_escapes(chunk)):
        if candidate is None:
            continue
        try:
            val = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(val, dict):
            yield val
            return


def _repair_common_llm_json_escapes(text: str) -> str | None:
    """Repair common quasi-JSON escape mistakes without changing valid JSON first-pass behavior."""
    if "\\'" not in text:
        return None
    # In JSON double-quoted strings, apostrophes do not need escaping. Some LLMs still emit
    # Python-style ``\'`` sequences, which are invalid JSON and would otherwise fail closed.
    return text.replace("\\'", "'")


def _iter_json_objects_via_raw_decode(s: str):
    """Every complete JSON object starting at any ``{`` (avoids first/last-brace slice errors)."""
    dec = json.JSONDecoder()
    for i, c in enumerate(s):
        if c != "{":
            continue
        try:
            val, _ = dec.raw_decode(s, i)
        except json.JSONDecodeError:
            continue
        if isinstance(val, dict):
            yield val
