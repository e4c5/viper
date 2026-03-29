"""Public runner entrypoint for code review orchestration."""

from __future__ import annotations

from code_review.orchestration_deps import *  # noqa: F401,F403
from code_review.review_orchestrator import ReviewOrchestrator


def run_review(
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str = "",
    *,
    dry_run: bool = False,
    print_findings: bool = False,
    review_decision_enabled: bool | None = None,
    review_decision_high_threshold: int | None = None,
    review_decision_medium_threshold: int | None = None,
    review_decision_only: bool = False,
    event_context: ReviewDecisionEventContext | None = None,
) -> list[FindingV1]:
    """
    Run the code review agent (findings-only mode). Fetches existing comments,
    runs agent, parses findings, filters by ignore list, and posts via provider.
    Returns list of findings that were posted (or would be posted if dry_run).

    Optional review-decision kwargs apply only to this run (they do not mutate
    the process-global cached :func:`~code_review.config.get_scm_config` instance).

    When *review_decision_only* is True (or ``CODE_REVIEW_REVIEW_DECISION_ONLY`` is set),
    skips the agent, inline posting, and idempotency short-circuit; only recomputes the
    quality gate and submits a PR review decision when enabled in SCM config.

    *event_context* may be supplied programmatically; when omitted, non-empty
    ``CODE_REVIEW_EVENT_*`` environment variables are parsed into
    :class:`~code_review.schemas.review_decision_event.ReviewDecisionEventContext`
    (used for review-decision-only logging and head SHA hints).
    """
    resolved_event = event_context or review_decision_event_context_from_env()
    orchestrator = ReviewOrchestrator(
        owner,
        repo,
        pr_number,
        head_sha,
        dry_run=dry_run,
        print_findings=print_findings,
        review_decision_enabled=review_decision_enabled,
        review_decision_high_threshold=review_decision_high_threshold,
        review_decision_medium_threshold=review_decision_medium_threshold,
        review_decision_only=review_decision_only,
        event_context=resolved_event,
    )
    return orchestrator.run()
