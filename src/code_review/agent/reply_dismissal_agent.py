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
You classify one pull-request review thread: an automated review comment and a human reply.

Your job: decide if the human has adequately addressed the concern raised in the review comment.

Output rules (critical):
- Respond with a single JSON object only (no prose before or after).
- You may wrap it in a markdown ```json code block if you prefer.
- Schema:
  - "verdict": "agreed" OR "disagreed"
  - "reply_text": string — required when verdict is "disagreed": a short, professional reply \
to post on the thread explaining why the concern still stands. Use empty string when verdict \
is "agreed".

Use "agreed" when the human fix, explanation, or tradeoff reasonably resolves the review \
comment. Use "disagreed" when the thread still needs action or the reply misses the point.

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
    raw = _parse_verdict_json_object(text)
    if raw is None:
        return None
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


def _parse_verdict_json_object(text: str) -> dict | None:
    """Extract a JSON object from model output (optional ```json fence)."""
    s = text.strip()
    chunks: list[str] = []
    fenced = _first_markdown_fence_body(s)
    if fenced is not None:
        chunks.append(fenced.strip())
    chunks.append(s)
    for chunk in chunks:
        try:
            val = json.loads(chunk)
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            continue
    return _first_json_object_via_raw_decode(s)


def _first_json_object_via_raw_decode(s: str) -> dict | None:
    """First complete JSON object starting at any ``{`` (avoids first/last-brace slice errors)."""
    dec = json.JSONDecoder()
    for i, c in enumerate(s):
        if c != "{":
            continue
        try:
            val, _ = dec.raw_decode(s, i)
        except json.JSONDecodeError:
            continue
        if isinstance(val, dict):
            return val
    return None
