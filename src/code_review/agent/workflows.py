"""ADK workflow prototypes for review execution."""

from __future__ import annotations

import logging

from code_review.agent.agent import _SHARED_TEST_QUALITY_RULES, create_review_agent
from code_review.batching import ReviewBatch
from code_review.config import get_code_review_app_config
from code_review.diff.parser import annotate_diff_with_line_numbers
from code_review.logging_config import emit_package_log
from code_review.providers.base import ProviderInterface
from code_review.standards.detector import is_test_file

logger = logging.getLogger(__name__)


def _batch_instruction_suffix(batch: ReviewBatch, head_sha: str) -> str:
    """Batch-specific instruction appended to each workflow sub-agent."""
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

    # IMPORTANT: Do NOT use bare {…} in this string — ADK's instruction template engine
    # matches the regex {+[^{}]*}+ and tries to substitute every such pattern from session
    # state.  Using bare braces (e.g. {n}: or {"findings": []}) causes a KeyError before
    # the LLM is ever called.  Use prose descriptions instead.
    suffix = (
        "For this run, ignore any generic wording about reviewing a complete PR diff "
        "in the user message. "
        "Review exactly one prepared batch from this PR."
        + head_sha_clause
        + f" This batch covers these file paths: {', '.join(batch.paths)}."
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
        suffix += "\n\n" + _SHARED_TEST_QUALITY_RULES
        logger.debug(
            "Appended test-quality rules to batch instruction (test paths: %s)",
            ", ".join(s.path for s in batch.segments if is_test_file(s.path)),
        )

    return suffix


def create_sequential_batch_review_agent(
    provider: ProviderInterface,
    review_standards: str,
    batches: list[ReviewBatch],
    *,
    head_sha: str = "",
    context_brief_attached: bool = False,
    review_visible_lines: bool | None = None,
):
    """Build a SequentialAgent that reviews prepared diff batches one after another."""
    from google.adk.agents import SequentialAgent

    sub_agents = []
    for index, batch in enumerate(batches):
        agent = create_review_agent(
            provider,
            review_standards,
            findings_only=True,
            disable_tools=True,
            context_brief_attached=context_brief_attached,
            review_visible_lines=review_visible_lines,
        )
        agent.name = f"batch_review_{index}"
        agent.instruction = agent.instruction.rstrip() + "\n\n" + _batch_instruction_suffix(
            batch, head_sha
        )
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

    return SequentialAgent(
        name="sequential_batch_review_agent",
        description="Batch-mode review: review prepared diff batches sequentially.",
        sub_agents=sub_agents,
    )
