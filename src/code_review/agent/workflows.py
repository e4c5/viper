"""ADK workflow prototypes for review execution."""

from __future__ import annotations

import logging
from contextlib import aclosing
from typing import TYPE_CHECKING

from google.adk.agents import BaseAgent
from google.genai import types
from pydantic import Field

from code_review.agent.agent import _SHARED_TEST_QUALITY_RULES, create_review_agent
from code_review.batching import ReviewBatch
from code_review.config import get_code_review_app_config
from code_review.diff.parser import annotate_diff_with_line_numbers
from code_review.logging_config import emit_package_log
from code_review.providers.base import ProviderInterface
from code_review.standards.detector import is_test_file

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events import Event

logger = logging.getLogger(__name__)


_BATCH_USER_MESSAGE_INSTRUCTION = """\
For this run, review exactly one prepared batch from this PR. The prepared batch,
PR metadata, and any linked-context supplement are provided in the user message.
Ignore any generic wording about reviewing a complete PR diff. Only report findings
for code that appears in the prepared batch segments in the user message."""


class BatchReviewWorkflowAgent(BaseAgent):
    """Run batch review sub-agents while giving each one its own user message."""

    batch_user_messages: list[types.Content] = Field(default_factory=list)
    current_index: int = Field(default=0)

    async def _run_async_impl(
        self, ctx: "InvocationContext"
    ) -> "AsyncGenerator[Event, None]":
        from google.adk.events import Event

        while self.current_index < len(self.sub_agents):
            index = self.current_index
            sub_agent = self.sub_agents[index]
            user_content = (
                self.batch_user_messages[index]
                if index < len(self.batch_user_messages)
                else ctx.user_content
            )
            if user_content and not user_content.role:
                user_content.role = "user"
            if user_content:
                await ctx.session_service.append_event(
                    session=ctx.session,
                    event=Event(
                        id=Event.new_id(),
                        invocation_id=ctx.invocation_id,
                        author="user",
                        branch=ctx.branch,
                        content=user_content,
                    ),
                )
            child_ctx = ctx.model_copy(update={"user_content": user_content})
            pause_invocation = False
            async with aclosing(sub_agent.run_async(child_ctx)) as agen:
                async for event in agen:
                    yield event
                    if ctx.should_pause_invocation(event):
                        pause_invocation = True
            if pause_invocation:
                return
            self.current_index += 1


def build_prepared_batch_user_message(
    *,
    batch: ReviewBatch,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str = "",
    prompt_suffix: str = "",
    retry_attempt: int = 0,
) -> str:
    """Build the user message for one prepared review batch."""
    head_sha_clause = f" head_sha={head_sha}." if head_sha else ""
    segment_blocks = []
    for segment in batch.segments:
        segment_label = f"{segment.segment_index + 1}/{segment.total_segments}"
        segment_scope = (
            f"partial segment {segment_label} for {segment.path}"
            if segment.total_segments > 1
            else f"full-file segment for {segment.path}"
        )
        annotated = annotate_diff_with_line_numbers(segment.diff_text)
        segment_blocks.append(
            f"Segment: {segment_scope}\n"
            f"Split strategy: {segment.split_strategy}\n"
            f"```diff\n{annotated}\n```"
        )

    message = (
        "Review exactly one prepared batch from this PR. "
        f"owner={owner}, repo={repo}, pr_number={pr_number}."
        + head_sha_clause
        + f" This batch covers these file paths: {', '.join(batch.paths)}."
    )
    if retry_attempt > 0:
        message += (
            "\n\nNote: Your previous response was interrupted and resulted in invalid, "
            "truncated JSON. "
            "Please be concise, omit overly long code snippets in the description, "
            "and ensure all JSON strings and arrays are fully closed."
        )
    if prompt_suffix:
        message += "\n\n" + prompt_suffix

    message += (
        "\n\n"
        "For this run, ignore any generic wording about reviewing a complete PR diff "
        "in the user message. "
        "Review exactly one prepared batch from this PR."
        + " Only report findings for code that appears in the batch segments below."
        + " Use the integer from the ``n:`` annotation as the line field in each finding"
        " (e.g. ``42:`` means line 42). Extract only the number; do NOT emit the ``:`` suffix."
        + " If a file appears in multiple segments, treat them as partial views "
        "of the same file and still use the true file path."
        + " Output a JSON findings object for this batch only"
          " (same schema as the main instruction)."
        + " If there are no issues in this batch, output a findings object with an empty array."
        + "\n\nPrepared batch segments:\n"
        + "\n\n".join(segment_blocks)
    )

    # Conditionally append test-quality rules when the batch contains test files.
    has_test_files = any(is_test_file(s.path) for s in batch.segments)
    if has_test_files:
        message += "\n\n" + _SHARED_TEST_QUALITY_RULES
        logger.debug(
            "Appended test-quality rules to batch user message (test paths: %s)",
            ", ".join(s.path for s in batch.segments if is_test_file(s.path)),
        )

    return message


def create_sequential_batch_review_agent(
    provider: ProviderInterface,
    review_standards: str,
    batches: list[ReviewBatch],
    *,
    context_brief_attached: bool = False,
    review_visible_lines: bool | None = None,
    use_output_key: bool = False,
):
    """Build a workflow agent that reviews prepared diff batches one after another."""

    sub_agents = []
    for index, batch in enumerate(batches):
        # output_key only safe for single-batch sessions: multiple sub-agents sharing
        # a session would overwrite each other's state entry.
        output_key = "findings_result" if (use_output_key and len(batches) == 1) else None
        agent = create_review_agent(
            provider,
            review_standards,
            context_brief_attached=context_brief_attached,
            review_visible_lines=review_visible_lines,
            slim_output=True,
            output_key=output_key,
        )
        agent.name = f"batch_review_{index}"
        agent.instruction = agent.instruction.rstrip() + "\n\n" + _BATCH_USER_MESSAGE_INSTRUCTION
        agent.include_contents = "none"
        if get_code_review_app_config().log_prompts:
            emit_package_log(
                logger,
                logging.INFO,
                "LLM instruction agent=%s prompt=%s",
                agent.name,
                agent.instruction,
            )
        # Prevent AutoFlow from adding the transfer_to_agent tool: each batch
        # sub-agent must return findings directly and must NOT transfer control
        # to peer or parent agents. Without these flags, ADK uses AutoFlow which
        # injects the transfer_to_agent function into the LLM call; the model
        # may invoke it instead of returning a JSON findings response, causing
        # base_llm_flow.run_async's while-True loop to spin indefinitely.
        agent.disallow_transfer_to_parent = True
        agent.disallow_transfer_to_peers = True
        logger.debug(
            "[batch] Registering sub-agent %s paths=%s segments=%d",
            agent.name,
            list(batch.paths),
            len(batch.segments),
        )
        sub_agents.append(agent)

    return BatchReviewWorkflowAgent(
        name="sequential_batch_review_agent",
        description="Batch-mode review: review prepared diff batches sequentially.",
        sub_agents=sub_agents,
    )
