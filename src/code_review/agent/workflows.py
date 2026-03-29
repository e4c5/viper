"""ADK workflow prototypes for review execution."""

from __future__ import annotations

from code_review.agent.agent import create_review_agent
from code_review.batching import ReviewBatch
from code_review.diff.parser import annotate_diff_with_line_numbers
from code_review.providers.base import ProviderInterface


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

    return (
        "For this run, ignore any generic wording about reviewing a complete PR diff in the user message. "
        "Review exactly one prepared batch from this PR."
        + head_sha_clause
        + f" This batch covers these file paths: {', '.join(batch.paths)}."
        + " Only report findings for code that appears in the batch segments below."
        + " Use the <L{n}> annotation value as the line field in each finding."
        + " If a file appears in multiple segments, treat them as partial views of the same file and still use the true file path."
        + ' Output a JSON object of the form {"findings": [...]} for this batch only.'
        + ' If there are no issues in this batch, output exactly {"findings": []}.'
        + "\n\nPrepared batch segments:\n"
        + "\n\n".join(segment_blocks)
    )


def create_sequential_batch_review_agent(
    provider: ProviderInterface,
    review_standards: str,
    batches: list[ReviewBatch],
    *,
    head_sha: str = "",
    context_brief_attached: bool = False,
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
        )
        agent.name = f"batch_review_{index}"
        agent.instruction = agent.instruction.rstrip() + "\n\n" + _batch_instruction_suffix(
            batch, head_sha
        )
        sub_agents.append(agent)

    return SequentialAgent(
        name="sequential_batch_review_agent",
        description="Batch-mode review: review prepared diff batches sequentially.",
        sub_agents=sub_agents,
    )
