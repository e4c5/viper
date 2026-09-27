"""Public runner entrypoint for code review orchestration."""

from __future__ import annotations

from code_review.config import CodeReviewAppConfig, LLMConfig, SCMConfig
from code_review.orchestration.orchestrator import ReviewOrchestrator
from code_review.schemas.findings import FindingV1
from code_review.schemas.review_decision_event import (
    ReviewDecisionConfig,
    ReviewDecisionEventContext,
    review_decision_event_context_from_env,
)

__all__ = [
    "run_review",
    "ReviewDecisionConfig",
    "ReviewDecisionEventContext",
    "FindingV1",
    "SCMConfig",
    "LLMConfig",
    "CodeReviewAppConfig",
]


def run_review(
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str = "",
    *,
    dry_run: bool = False,
    print_findings: bool = False,
    review_decision: ReviewDecisionConfig | None = None,
    scm_config: SCMConfig | None = None,
    llm_config: LLMConfig | None = None,
    app_config: CodeReviewAppConfig | None = None,
) -> list[FindingV1]:
    """
    Run the code review agent (findings-only mode). Fetches existing comments,
    runs agent, parses findings, filters by ignore list, and posts via provider.
    Returns list of findings that were posted (or would be posted if dry_run).

    *review_decision* groups per-run overrides (enabled flag, thresholds, only-mode,
    and event context). These apply only to this run and do not mutate the
    process-global cached :func:`~code_review.config.get_scm_config` instance.

    When ``review_decision.only`` is True (or ``CODE_REVIEW_REVIEW_DECISION_ONLY`` is set),
    skips the agent, inline posting, and idempotency short-circuit; only recomputes the
    quality gate and submits a PR review decision when enabled in SCM config.

    *scm_config*, *llm_config*, and *app_config* may be supplied programmatically to bypass
    process environment loading. When omitted, the existing environment-driven
    configuration path is preserved.
    """
    import dataclasses

    rd = review_decision or ReviewDecisionConfig()
    resolved_event = rd.event_context or review_decision_event_context_from_env()
    orchestrator = ReviewOrchestrator(
        owner,
        repo,
        pr_number,
        head_sha,
        dry_run=dry_run,
        print_findings=print_findings,
        review_decision=dataclasses.replace(rd, event_context=resolved_event),
        scm_config=scm_config,
        llm_config=llm_config,
        app_config=app_config,
    )
    return orchestrator.run()
