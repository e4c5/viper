from __future__ import annotations

import logging

from google.genai import types

from code_review import orchestration_deps as runner_mod
from code_review.batching import ReviewBatch, build_review_batches
from code_review.diff.utils import estimate_tokens
from code_review.logging_config import emit_package_log
from code_review.models import PRContext

logger = logging.getLogger(__name__)


def run_agent_and_collect_response(
    runner, session_service, session_id: str, content: types.Content
) -> str:
    """Run an agent once and return the concatenated final response text."""
    del session_service
    return runner_mod._run_agent_and_collect_response(runner, session_id, content)


def create_agent_and_runner(
    pr_ctx: PRContext,
    provider,
    review_standards: str,
    batches: list[ReviewBatch],
    *,
    context_brief_attached: bool = False,
):
    """Build the batch-review SequentialAgent, session service, and ADK Runner."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    from code_review.agent.workflows import create_sequential_batch_review_agent

    agent = create_sequential_batch_review_agent(
        provider,
        review_standards,
        batches,
        head_sha=pr_ctx.head_sha,
        context_brief_attached=context_brief_attached,
    )
    session_id = f"{pr_ctx.owner}/{pr_ctx.repo}/pr-{pr_ctx.pr_number}/{runner_mod.uuid.uuid4().hex[:12]}"
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name=runner_mod.APP_NAME,
        session_service=session_service,
        auto_create_session=True,
    )
    return (session_id, session_service, runner)


def run_agent_and_collect_findings(
    pr_ctx: PRContext,
    provider,
    review_standards: str,
    runner,
    session_id: str,
    batches: list[ReviewBatch],
    *,
    context_brief_attached: bool = False,
    prompt_suffix: str = "",
) -> list[runner_mod.FindingV1]:
    """Run batch review and parse responses into findings."""
    if not batches:
        return []
    return _run_sequential_batch_review_mode(
        pr_ctx,
        provider,
        review_standards,
        runner,
        session_id,
        batches=batches,
        batch_count=len(batches),
        context_brief_attached=context_brief_attached,
        prompt_suffix=prompt_suffix,
    )


def _run_sequential_batch_review_mode(
    pr_ctx: PRContext,
    provider,
    review_standards: str,
    runner,
    session_id: str,
    *,
    batches: list[ReviewBatch],
    batch_count: int,
    context_brief_attached: bool = False,
    prompt_suffix: str = "",
) -> list[runner_mod.FindingV1]:
    """Run the SequentialAgent batch workflow and preserve successful batches on rate limit."""
    content = build_batch_review_content(
        pr_ctx=pr_ctx,
        batch_count=batch_count,
        prompt_suffix=prompt_suffix,
    )
    logger.info(
        "[batch] Invoking SequentialAgent runner: session=%s batch_count=%d",
        session_id,
        batch_count,
    )
    try:
        responses = runner_mod._run_agent_and_collect_responses(
            runner, session_id, content
        )
    except runner_mod.PartialResponseCollectionError as exc:
        if isinstance(exc.cause, runner_mod.RateLimitError):
            return _recover_rate_limited_batches(
                pr_ctx,
                provider,
                review_standards,
                batches,
                completed_responses=exc.responses,
                context_brief_attached=context_brief_attached,
                prompt_suffix=prompt_suffix,
                error=exc.cause,
            )
        raise exc.cause from exc
    logger.info(
        "[batch] SequentialAgent runner returned: session=%s responses=%d",
        session_id,
        len(responses),
    )
    return findings_from_batch_responses(responses)


def build_batch_review_content(
    *,
    pr_ctx: PRContext,
    batch_count: int,
    prompt_suffix: str = "",
):
    """Build the user message used to execute a prepared batch-review workflow."""
    msg = (
        "Review the prepared PR batches sequentially. "
        f"owner={pr_ctx.owner}, repo={pr_ctx.repo}, pr_number={pr_ctx.pr_number}."
        + (f" head_sha={pr_ctx.head_sha}." if pr_ctx.head_sha else "")
        + f" Prepared batch count: {batch_count}."
    )
    if prompt_suffix:
        msg += "\n\n" + prompt_suffix
    if runner_mod.get_code_review_app_config().log_prompts:
        emit_package_log(
            runner_mod.logger,
            logging.INFO,
            "LLM user prompt session=%s prompt=%s",
            "<dynamic>",
            msg,
        )
    elif runner_mod.logger.isEnabledFor(runner_mod.logging.DEBUG):
        runner_mod.logger.debug(
            "LLM request (batch SequentialAgent) session=%s prompt=%s",
            "<dynamic>",
            msg,
        )
    return runner_mod.types.Content(role="user", parts=[runner_mod.types.Part(text=msg)])


def findings_from_batch_responses(
    responses: list[tuple[str, str]],
) -> list[runner_mod.FindingV1]:
    """Parse structured findings from a list of batch response texts."""
    all_findings: list[runner_mod.FindingV1] = []
    for _author, response_text in responses:
        all_findings.extend(runner_mod._findings_from_response(response_text))
    return all_findings


def batch_index_from_author(author: str) -> int | None:
    """Extract the original batch index from a workflow response author name."""
    prefix = "batch_review_"
    if not author.startswith(prefix):
        return None
    suffix = author[len(prefix) :]
    return int(suffix) if suffix.isdigit() else None


def _recover_rate_limited_batches(
    pr_ctx: PRContext,
    provider,
    review_standards: str,
    batches: list[ReviewBatch],
    *,
    completed_responses: list[tuple[str, str]],
    context_brief_attached: bool,
    prompt_suffix: str,
    error: runner_mod.RateLimitError,
) -> list[runner_mod.FindingV1]:
    """Keep successful batch responses and isolate the remaining batches one-by-one."""
    completed_batch_indexes = {
        batch_index
        for author, _text in completed_responses
        if (batch_index := batch_index_from_author(author)) is not None
    }
    runner_mod.logger.warning(
        "Batch review hit rate limit after %d/%d completed batch response(s); "
        "continuing remaining batches individually: %s",
        len(completed_batch_indexes),
        len(batches),
        error,
    )
    all_findings = findings_from_batch_responses(completed_responses)
    for batch_index, batch in enumerate(batches):
        if batch_index in completed_batch_indexes:
            continue
        session_id, _, runner = create_agent_and_runner(
            pr_ctx,
            provider,
            review_standards,
            [batch],
            context_brief_attached=context_brief_attached,
        )
        content = build_batch_review_content(
            pr_ctx=pr_ctx,
            batch_count=1,
            prompt_suffix=prompt_suffix,
        )
        try:
            responses = runner_mod._run_agent_and_collect_responses(
                runner, session_id, content
            )
        except runner_mod.PartialResponseCollectionError as exc:
            if isinstance(exc.cause, runner_mod.RateLimitError):
                runner_mod.logger.warning(
                    "Skipping rate-limited batch %d/%d paths=%s: %s",
                    batch_index + 1,
                    len(batches),
                    ", ".join(batch.paths),
                    exc.cause,
                )
                continue
            raise exc.cause from exc
        all_findings.extend(findings_from_batch_responses(responses))
    return all_findings


def build_review_batches_for_scope(
    files: list[object], paths: list[str], full_diff: str, diff_budget: int
) -> list[ReviewBatch]:
    """Slice the scoped diff by file and pack the resulting segments into ordered batches."""
    scoped_diff_by_path = {
        path: runner_mod.unified_diff_for_path(full_diff, path) for path in paths
    }
    effective_diff_budget = diff_budget
    if effective_diff_budget <= 0:
        effective_diff_budget = max(
            (
                estimate_tokens(diff_text)
                for diff_text in scoped_diff_by_path.values()
                if diff_text.strip()
            ),
            default=1,
        )
        logger.warning(
            "Computed diff budget %d is too small for batching; "
            "falling back to max single-file diff estimate %d",
            diff_budget,
            effective_diff_budget,
        )
    return build_review_batches(
        files,
        scoped_diff_by_path,
        diff_budget_tokens=effective_diff_budget,
    )


def log_review_batch_plan(
    batches: list[ReviewBatch], paths: list[str], incremental_base_sha: str
) -> None:
    """Emit a concise log line describing the prepared review batches."""
    segment_count = sum(len(batch.segments) for batch in batches)
    mode_label = "incremental batch mode" if incremental_base_sha else "batch mode"
    runner_mod.logger.info(
        "Running agent on %d file(s) across %d batch(es) and %d segment(s) (%s)",
        len(paths),
        len(batches),
        segment_count,
        mode_label,
    )
