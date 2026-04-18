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
    review_visible_lines: bool | None = None,
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
        review_visible_lines=review_visible_lines,
    )
    session_id = (
        f"{pr_ctx.owner}/{pr_ctx.repo}/pr-{pr_ctx.pr_number}"
        f"/{runner_mod.uuid.uuid4().hex[:12]}"
    )
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
    review_visible_lines: bool | None = None,
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
        review_visible_lines=review_visible_lines,
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
    review_visible_lines: bool | None = None,
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
            response_indexes = {
                idx
                for author, _ in exc.responses
                if (idx := batch_index_from_author(author)) is not None
            }
            logger.warning(
                "Batch review hit rate limit after %d/%d completed batch response(s); "
                "continuing remaining batches individually: %s",
                len(response_indexes),
                batch_count,
                exc.cause,
            )
            findings, failed_indexes = findings_from_batch_responses(exc.responses)
            failed_set = set(failed_indexes)
            completed_successfully = response_indexes - failed_set
            failed_batches = [batches[i] for i in failed_indexes if i < len(batches)]
            remaining_batches = [
                b for i, b in enumerate(batches) if i not in completed_successfully and i not in failed_set
            ]
            if failed_batches:
                logger.warning(
                    "Recovering %d completed batch(es) that returned malformed findings before the rate limit.",
                    len(failed_batches),
                )
                findings.extend(
                    _run_isolated_batches_with_retry(
                        pr_ctx,
                        provider,
                        review_standards,
                        failed_batches,
                        context_brief_attached=context_brief_attached,
                        prompt_suffix=prompt_suffix,
                        review_visible_lines=review_visible_lines,
                        initial_retry_attempt=1,
                    )
                )
            findings.extend(
                _run_isolated_batches_with_retry(
                    pr_ctx,
                    provider,
                    review_standards,
                    remaining_batches,
                    context_brief_attached=context_brief_attached,
                    prompt_suffix=prompt_suffix,
                    review_visible_lines=review_visible_lines,
                )
            )
            return findings
        raise exc.cause from exc
    logger.info(
        "[batch] SequentialAgent runner returned: session=%s responses=%d",
        session_id,
        len(responses),
    )

    findings, failed_indexes = findings_from_batch_responses(responses)
    if failed_indexes:
        logger.warning("Recovering %d batch(es) that failed JSON parsing.", len(failed_indexes))
        failed_batches = [batches[i] for i in failed_indexes if i < len(batches)]
        findings.extend(
            _run_isolated_batches_with_retry(
                pr_ctx,
                provider,
                review_standards,
                failed_batches,
                context_brief_attached=context_brief_attached,
                prompt_suffix=prompt_suffix,
                review_visible_lines=review_visible_lines,
                initial_retry_attempt=1,
            )
        )
    return findings



def build_batch_review_content(
    *,
    pr_ctx: PRContext,
    batch_count: int,
    prompt_suffix: str = "",
    retry_attempt: int = 0,
):
    """Build the user message used to execute a prepared batch-review workflow."""
    msg = (
        "Review the prepared PR batches sequentially. "
        f"owner={pr_ctx.owner}, repo={pr_ctx.repo}, pr_number={pr_ctx.pr_number}."
        + (f" head_sha={pr_ctx.head_sha}." if pr_ctx.head_sha else "")
        + f" Prepared batch count: {batch_count}."
    )
    if retry_attempt > 0:
        msg += (
            "\n\nNote: Your previous response was interrupted and resulted in invalid, truncated JSON. "
            "Please be concise, omit overly long code snippets in the description, "
            "and ensure all JSON strings and arrays are fully closed."
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
) -> tuple[list[runner_mod.FindingV1], list[int]]:
    """Parse structured findings from a list of batch response texts, returning (findings, failed_indexes)."""
    all_findings: list[runner_mod.FindingV1] = []
    failed_indexes: list[int] = []
    for author, response_text in responses:
        try:
            all_findings.extend(
                runner_mod._findings_from_response(response_text, raise_errors=True)
            )
        except ValueError as e:
            idx = batch_index_from_author(author)
            runner_mod.logger.warning("Batch %s response failed to parse: %s", idx, e)
            if idx is not None:
                failed_indexes.append(idx)
    return all_findings, failed_indexes


def batch_index_from_author(author: str) -> int | None:
    """Extract the original batch index from a workflow response author name."""
    prefix = "batch_review_"
    if not author.startswith(prefix):
        return None
    suffix = author[len(prefix) :]
    return int(suffix) if suffix.isdigit() else None


def _run_isolated_batches_with_retry(
    pr_ctx: PRContext,
    provider,
    review_standards: str,
    batches_to_run: list[ReviewBatch],
    *,
    context_brief_attached: bool,
    prompt_suffix: str,
    review_visible_lines: bool | None = None,
    initial_retry_attempt: int = 0,
    max_retries: int = 2,
) -> list[runner_mod.FindingV1]:
    """Run specified batches individually with retries for rate limits or parse failures."""
    all_findings = []
    for batch in batches_to_run:
        batch_findings = []
        for attempt in range(initial_retry_attempt, max_retries + 1):
            session_id, _, runner = create_agent_and_runner(
                pr_ctx,
                provider,
                review_standards,
                [batch],
                context_brief_attached=context_brief_attached,
                review_visible_lines=review_visible_lines,
            )
            content = build_batch_review_content(
                pr_ctx=pr_ctx,
                batch_count=1,
                prompt_suffix=prompt_suffix,
                retry_attempt=attempt,
            )
            try:
                responses = runner_mod._run_agent_and_collect_responses(
                    runner, session_id, content
                )
            except runner_mod.PartialResponseCollectionError as exc:
                if isinstance(exc.cause, runner_mod.RateLimitError):
                    runner_mod.logger.warning(
                        "Rate-limited on batch paths=%s (attempt %d/%d): %s",
                        ", ".join(batch.paths),
                        attempt + 1,
                        max_retries + 1,
                        exc.cause,
                    )
                    if attempt == max_retries:
                        runner_mod.logger.warning(
                            "Skipping batch after max retries due to rate limits."
                        )
                    continue
                raise exc.cause from exc

            findings, failed_indexes = findings_from_batch_responses(responses)
            if failed_indexes:
                runner_mod.logger.warning(
                    "JSON parse failed for batch paths=%s (attempt %d/%d).",
                    ", ".join(batch.paths),
                    attempt + 1,
                    max_retries + 1,
                )
                if attempt < max_retries:
                    continue

            # Success or max retries exhausted
            batch_findings.extend(findings)
            break

        all_findings.extend(batch_findings)
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
