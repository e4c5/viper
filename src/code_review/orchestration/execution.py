from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from google.genai import types

from code_review import orchestration_deps as runner_mod
from code_review.batching import (
    ReviewBatch,
    ReviewSegment,
    build_review_batches,
    split_file_diff_into_segments,
)
from code_review.config import LLMConfig
from code_review.diff.utils import estimate_tokens
from code_review.logging_config import emit_package_log
from code_review.models import PRContext

logger = logging.getLogger(__name__)

# Module-level so tests can patch it; retry sleeps must never really sleep in tests.
_sleep = time.sleep


@dataclass(frozen=True)
class BatchReviewOutcome:
    """Findings plus the paths of prepared batches that went unreviewed."""

    findings: list[runner_mod.FindingV1] = field(default_factory=list)
    unreviewed_paths: tuple[str, ...] = ()

    @property
    def coverage_complete(self) -> bool:
        return not self.unreviewed_paths


def _unreviewed_paths(batches: list[ReviewBatch]) -> tuple[str, ...]:
    """De-duplicated, ordered union of .paths across abandoned batches."""
    return tuple(dict.fromkeys(path for batch in batches for path in batch.paths))


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
    single_batch_mode: bool = False,
    llm_config: LLMConfig | None = None,
):
    """Build the batch-review SequentialAgent, session service, and ADK Runner."""
    from google.adk.sessions import InMemorySessionService

    from code_review.adk_runner import create_runner
    from code_review.agent.workflows import create_sequential_batch_review_agent

    agent_kwargs = {
        "context_brief_attached": context_brief_attached,
        "review_visible_lines": review_visible_lines,
        "use_output_key": single_batch_mode,
    }
    if llm_config is not None:
        agent_kwargs["llm_config"] = llm_config
    agent = create_sequential_batch_review_agent(
        provider,
        review_standards,
        batches,
        **agent_kwargs,
    )
    session_id = (
        f"{pr_ctx.owner}/{pr_ctx.repo}/pr-{pr_ctx.pr_number}/{runner_mod.uuid.uuid4().hex[:12]}"
    )
    session_service = InMemorySessionService()
    runner = create_runner(
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
    llm_config: LLMConfig | None = None,
) -> BatchReviewOutcome:
    """Run batch review and parse responses into findings."""
    if not batches:
        return BatchReviewOutcome([])
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
        llm_config=llm_config,
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
    llm_config: LLMConfig | None = None,
) -> BatchReviewOutcome:
    """Run the SequentialAgent batch workflow, preserving completed batches on transient errors."""
    _attach_batch_user_messages(
        runner,
        pr_ctx=pr_ctx,
        batches=batches,
        prompt_suffix=prompt_suffix,
    )
    content = build_batch_review_content(
        pr_ctx=pr_ctx,
        batch_count=batch_count,
    )
    logger.info(
        "[batch] Invoking SequentialAgent runner: session=%s batch_count=%d",
        session_id,
        batch_count,
    )
    effective_llm_config = llm_config or runner_mod.get_llm_config()
    try:
        responses = runner_mod._run_agent_and_collect_responses(
            runner,
            session_id,
            content,
            idle_timeout_seconds=effective_llm_config.timeout_seconds,
        )
    except runner_mod.PartialResponseCollectionError as exc:
        if runner_mod.is_transient_llm_error(exc.cause):
            response_indexes = {
                idx
                for author, _ in exc.responses
                if (idx := batch_index_from_author(author)) is not None
            }
            logger.warning(
                "Batch review hit transient LLM error (%s) after %d/%d completed batch "
                "response(s); continuing remaining batches individually: %s",
                type(exc.cause).__name__,
                len(response_indexes),
                batch_count,
                exc.cause,
            )
            findings, failed_indexes = findings_from_batch_responses(exc.responses)
            failed_set = set(failed_indexes)
            completed_successfully = response_indexes - failed_set
            failed_batches = [batches[i] for i in failed_indexes if i < len(batches)]
            remaining_batches = [
                b
                for i, b in enumerate(batches)
                if i not in completed_successfully and i not in failed_set
            ]
            unreviewed: list[ReviewBatch] = []
            if failed_batches:
                logger.warning(
                    "Recovering %d completed batch(es) that returned malformed findings "
                    "before the transient LLM error.",
                    len(failed_batches),
                )
                retry_findings, retry_unreviewed = _run_isolated_batches_with_retry(
                    pr_ctx,
                    provider,
                    review_standards,
                    failed_batches,
                    context_brief_attached=context_brief_attached,
                    prompt_suffix=prompt_suffix,
                    review_visible_lines=review_visible_lines,
                    llm_config=llm_config,
                    initial_retry_attempt=1,
                )
                findings.extend(retry_findings)
                unreviewed.extend(retry_unreviewed)
            retry_findings, retry_unreviewed = _run_isolated_batches_with_retry(
                pr_ctx,
                provider,
                review_standards,
                remaining_batches,
                context_brief_attached=context_brief_attached,
                prompt_suffix=prompt_suffix,
                review_visible_lines=review_visible_lines,
                llm_config=llm_config,
            )
            findings.extend(retry_findings)
            unreviewed.extend(retry_unreviewed)
            return BatchReviewOutcome(findings, _unreviewed_paths(unreviewed))
        raise exc.cause from exc
    logger.info(
        "[batch] SequentialAgent runner returned: session=%s responses=%d",
        session_id,
        len(responses),
    )

    findings, failed_indexes = findings_from_batch_responses(responses)
    failed_indexes.extend(
        idx
        for idx in missing_batch_response_indexes(responses, batch_count)
        if idx not in failed_indexes
    )
    unreviewed: list[ReviewBatch] = []
    if failed_indexes:
        logger.warning(
            "Recovering %d batch(es) that failed JSON parsing or did not return a "
            "text-bearing final response.",
            len(failed_indexes),
        )
        failed_batches = [batches[i] for i in failed_indexes if i < len(batches)]
        retry_findings, unreviewed = _run_isolated_batches_with_retry(
            pr_ctx,
            provider,
            review_standards,
            failed_batches,
            context_brief_attached=context_brief_attached,
            prompt_suffix=prompt_suffix,
            review_visible_lines=review_visible_lines,
            llm_config=llm_config,
            initial_retry_attempt=1,
        )
        findings.extend(retry_findings)
    return BatchReviewOutcome(findings, _unreviewed_paths(unreviewed))


def build_batch_review_content(
    *,
    pr_ctx: PRContext,
    batch_count: int,
    prompt_suffix: str = "",
    retry_attempt: int = 0,
):
    """Build the root user message used to execute a prepared batch-review workflow."""
    msg = (
        "Review the prepared PR batches sequentially. "
        f"owner={pr_ctx.owner}, repo={pr_ctx.repo}, pr_number={pr_ctx.pr_number}."
        + (f" head_sha={pr_ctx.head_sha}." if pr_ctx.head_sha else "")
        + f" Prepared batch count: {batch_count}. "
        "Each batch reviewer receives its prepared diff payload as a separate user message."
    )
    if retry_attempt > 0:
        msg += (
            "\n\nNote: Your previous response was interrupted and resulted in invalid, "
            "truncated JSON. "
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


def _attach_batch_user_messages(
    runner,
    *,
    pr_ctx: PRContext,
    batches: list[ReviewBatch],
    prompt_suffix: str = "",
    retry_attempt: int = 0,
) -> None:
    """Attach one cache-friendly user message per prepared batch to the workflow agent."""
    from code_review.agent.workflows import build_prepared_batch_user_message

    agent = _batch_message_agent(getattr(runner, "agent", None))
    if agent is None:
        logger.warning(
            "Unable to attach batch user messages; runner agent does not expose "
            "batch_user_messages (agent_type=%s)",
            type(getattr(runner, "agent", None)).__name__,
        )
        return
    agent.batch_user_messages = [
        runner_mod.types.Content(
            role="user",
            parts=[
                runner_mod.types.Part(
                    text=build_prepared_batch_user_message(
                        batch=batch,
                        owner=pr_ctx.owner,
                        repo=pr_ctx.repo,
                        pr_number=pr_ctx.pr_number,
                        head_sha=pr_ctx.head_sha,
                        prompt_suffix=prompt_suffix,
                        retry_attempt=retry_attempt,
                    )
                )
            ],
        )
        for batch in batches
    ]


def _batch_message_agent(agent):
    """Return the nested workflow agent that accepts prepared batch user messages."""
    seen: set[int] = set()
    while agent is not None and id(agent) not in seen:
        if hasattr(agent, "batch_user_messages"):
            return agent
        seen.add(id(agent))
        agent = getattr(agent, "agent", None)
    return None


def findings_from_batch_responses(
    responses: list[tuple[str, str]],
) -> tuple[list[runner_mod.FindingV1], list[int]]:
    """Parse batch response texts and return findings plus failed batch indexes."""
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


def missing_batch_response_indexes(responses: list[tuple[str, str]], batch_count: int) -> list[int]:
    """Return batch indexes that did not emit a text-bearing final response."""
    if not responses:
        return list(range(batch_count))
    seen = {
        idx
        for author, _response_text in responses
        if (idx := batch_index_from_author(author)) is not None
    }
    if not seen:
        return list(range(batch_count))
    return [idx for idx in range(batch_count) if idx not in seen]


def _make_retry_batch(batch_index: int, segments: tuple[ReviewSegment, ...]) -> ReviewBatch:
    """Build a retry batch from a subset of review segments."""
    return ReviewBatch(
        batch_index=batch_index,
        estimated_tokens=sum(segment.estimated_tokens for segment in segments),
        segments=segments,
        paths=tuple(dict.fromkeys(segment.path for segment in segments)),
    )


def _split_batch_for_retry(
    batch: ReviewBatch,
    *,
    attempt: int,
    max_retries: int,
    token_counter: Callable[[str], int] = estimate_tokens,
) -> list[tuple[ReviewBatch, int]]:
    """Return smaller retry batches when a batch's response is malformed.

    Prefer splitting across existing prepared segments first. If only a single segment
    remains, try to re-segment its diff text with a smaller budget. If neither produces
    smaller work units, return the original batch unchanged.

    Splitting itself consumes a retry attempt, the same as a plain re-request does.
    This is what bounds the "keep halving and re-splitting" path: once `attempt` has
    reached `max_retries`, no further splitting is attempted here and the batch is
    returned as a single unchanged unit so the caller's existing retry-exhaustion
    branch (skip + log "Skipping batch after max retries...") takes over instead of
    resplitting forever.
    """
    retry_attempt = min(attempt, max_retries)
    if attempt >= max_retries:
        return [(batch, retry_attempt)]

    next_attempt = min(attempt + 1, max_retries)

    if len(batch.segments) > 1:
        midpoint = len(batch.segments) // 2
        return [
            (_make_retry_batch(0, batch.segments[:midpoint]), next_attempt),
            (_make_retry_batch(1, batch.segments[midpoint:]), next_attempt),
        ]

    if len(batch.segments) != 1:
        return [(batch, retry_attempt)]

    segment = batch.segments[0]
    if segment.estimated_tokens <= 1:
        return [(batch, retry_attempt)]
    smaller_budget = max(1, segment.estimated_tokens // 2)
    smaller_segments = split_file_diff_into_segments(
        segment.path,
        segment.diff_text,
        segment_budget_tokens=smaller_budget,
        token_counter=token_counter,
    )
    if len(smaller_segments) <= 1:
        return [(batch, retry_attempt)]
    return [
        (_make_retry_batch(index, (smaller_segment,)), next_attempt)
        for index, smaller_segment in enumerate(smaller_segments)
    ]


def _run_retry_batch(
    pr_ctx: PRContext,
    provider,
    review_standards: str,
    batch: ReviewBatch,
    *,
    context_brief_attached: bool,
    prompt_suffix: str,
    review_visible_lines: bool | None,
    llm_config: LLMConfig | None,
    attempt: int,
    idle_timeout_seconds: float | None = None,
) -> tuple[list[runner_mod.FindingV1], bool, Exception | None]:
    """Run one retry batch and return findings, malformed-output flag, transient error."""
    session_id, _session_service, runner = create_agent_and_runner(
        pr_ctx,
        provider,
        review_standards,
        [batch],
        context_brief_attached=context_brief_attached,
        review_visible_lines=review_visible_lines,
        llm_config=llm_config,
    )
    _attach_batch_user_messages(
        runner,
        pr_ctx=pr_ctx,
        batches=[batch],
        prompt_suffix=prompt_suffix,
        retry_attempt=attempt,
    )
    content = build_batch_review_content(pr_ctx=pr_ctx, batch_count=1, retry_attempt=attempt)
    try:
        responses = runner_mod._run_agent_and_collect_responses(
            runner,
            session_id,
            content,
            idle_timeout_seconds=idle_timeout_seconds,
        )
    except runner_mod.PartialResponseCollectionError as exc:
        if runner_mod.is_transient_llm_error(exc.cause):
            return [], False, exc.cause
        raise exc.cause from exc

    findings, failed_indexes = findings_from_batch_responses(responses)
    missing_indexes = missing_batch_response_indexes(responses, 1)
    failed_indexes.extend(index for index in missing_indexes if index not in failed_indexes)
    return findings, bool(failed_indexes or not responses), None


def _run_isolated_batches_with_retry(
    pr_ctx: PRContext,
    provider,
    review_standards: str,
    batches_to_run: list[ReviewBatch],
    *,
    context_brief_attached: bool,
    prompt_suffix: str,
    review_visible_lines: bool | None = None,
    llm_config: LLMConfig | None = None,
    initial_retry_attempt: int = 0,
    max_retries: int = 2,
) -> tuple[list[runner_mod.FindingV1], list[ReviewBatch]]:
    """Run specified batches individually with adaptive retries and scope shrinking.

    Returns (findings, batches abandoned after retry exhaustion). Transient LLM
    errors are retried up to ``LLMConfig.max_retries`` times per batch with
    backoff, tracked separately from the malformed-output attempt counter.
    """
    effective_llm_config = llm_config or runner_mod.get_llm_config()
    max_transient_retries = max(0, int(getattr(effective_llm_config, "max_retries", 3)))
    idle_timeout_seconds = getattr(effective_llm_config, "timeout_seconds", None)
    all_findings: list[runner_mod.FindingV1] = []
    unreviewed: list[ReviewBatch] = []
    # (batch, malformed_attempt, transient_attempt)
    pending: list[tuple[ReviewBatch, int, int]] = [
        (batch, initial_retry_attempt, 0) for batch in batches_to_run
    ]
    while pending:
        batch, attempt, transient_attempt = pending.pop(0)
        findings, malformed, transient_exc = _run_retry_batch(
            pr_ctx,
            provider,
            review_standards,
            batch,
            context_brief_attached=context_brief_attached,
            prompt_suffix=prompt_suffix,
            review_visible_lines=review_visible_lines,
            llm_config=llm_config,
            attempt=attempt,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        if transient_exc is not None:
            runner_mod.logger.warning(
                "Transient LLM error (%s) on batch paths=%s (transient attempt %d/%d).",
                type(transient_exc).__name__,
                ", ".join(batch.paths),
                transient_attempt + 1,
                max_transient_retries + 1,
            )
            if transient_attempt < max_transient_retries:
                delay = runner_mod.retry_after_seconds(transient_exc)
                if delay is None:
                    delay = min(60.0, 2.0 ** transient_attempt) + random.uniform(0, 1)
                _sleep(min(120.0, delay))
                pending.insert(0, (batch, attempt, transient_attempt + 1))
            else:
                runner_mod.logger.warning(
                    "Skipping batch after max transient LLM retries (%s).",
                    type(transient_exc).__name__,
                )
                unreviewed.append(batch)
            continue
        if not malformed:
            all_findings.extend(findings)
            continue

        runner_mod.logger.warning(
            "JSON parse failed or no batch response was collected for batch paths=%s "
            "(attempt %d/%d).",
            ", ".join(batch.paths),
            attempt + 1,
            max_retries + 1,
        )
        smaller_batches = _split_batch_for_retry(batch, attempt=attempt, max_retries=max_retries)
        if len(smaller_batches) > 1:
            runner_mod.logger.warning(
                "Splitting malformed batch paths=%s segments=%d into %d smaller batch(es).",
                ", ".join(batch.paths),
                len(batch.segments),
                len(smaller_batches),
            )
            pending = [
                (smaller_batch, smaller_attempt, 0)
                for smaller_batch, smaller_attempt in smaller_batches
            ] + pending
            continue
        if attempt < max_retries:
            pending.insert(0, (batch, attempt + 1, transient_attempt))
            continue
        runner_mod.logger.warning(
            "Skipping batch after max retries due to malformed findings output."
        )
        unreviewed.append(batch)
    return all_findings, unreviewed


def build_review_batches_for_scope(
    files: list[object],
    paths: list[str],
    full_diff: str,
    diff_budget: int,
    token_counter: Callable[[str], int] = estimate_tokens,
) -> list[ReviewBatch]:
    """Slice the scoped diff by file and pack the resulting segments into ordered batches."""
    scoped_diff_by_path = {
        path: runner_mod.unified_diff_for_path(full_diff, path) for path in paths
    }
    effective_diff_budget = diff_budget
    if effective_diff_budget <= 0:
        effective_diff_budget = max(
            (
                token_counter(diff_text)
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
        token_counter=token_counter,
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
