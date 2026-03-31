"""Quality gate: aggregated open high/medium counts and derived PR review decision."""

from __future__ import annotations

import logging
from typing import Any

from code_review.diff.fingerprint import parse_marker_from_comment_body
from code_review.providers.base import UnresolvedReviewItem
from code_review.quality.outcome import QualityGateOutcome
from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)


def _quality_gate_dedupe_key_for_item(item: UnresolvedReviewItem) -> str:
    """Stable key so the same issue is not double-counted (marker fingerprint preferred)."""
    parsed = parse_marker_from_comment_body(item.body)
    fp = parsed.get("fingerprint")
    if fp:
        return f"fp:{fp}"
    if item.thread_id:
        return f"thread:{item.thread_id}"
    return f"id:{item.stable_id}"


def _quality_gate_dedupe_key_for_new_finding(finding: FindingV1, fp: str) -> str:
    if fp:
        return f"fp:{fp}"
    return f"new:{finding.path}:{finding.line}:{finding.code}"


def _quality_gate_fetch_unresolved_items(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
) -> list[Any] | None:
    try:
        items = provider.get_unresolved_review_items_for_quality_gate(owner, repo, pr_number)
    except Exception as e:
        logger.warning(
            "get_unresolved_review_items_for_quality_gate failed owner=%s repo=%s pr_number=%s: %s",
            owner,
            repo,
            pr_number,
            e,
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        return None
    return items if isinstance(items, list) else []


def _quality_gate_bump_seen(
    seen_keys: set[str],
    high_count: int,
    medium_count: int,
    key: str,
    severity: str,
) -> tuple[int, int]:
    if key in seen_keys:
        return high_count, medium_count
    seen_keys.add(key)
    if severity == "high":
        return high_count + 1, medium_count
    if severity == "medium":
        return high_count, medium_count + 1
    return high_count, medium_count


def _quality_gate_high_medium_counts(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    to_post: list[tuple[FindingV1, str]],
    *,
    excluded_stable_ids: frozenset[str] | None = None,
) -> tuple[int, int] | None:
    """Count distinct open high/medium signals: existing unresolved items plus net-new findings."""
    items = _quality_gate_fetch_unresolved_items(provider, owner, repo, pr_number)
    if items is None:
        return None
    skip_ids = excluded_stable_ids or frozenset()
    seen_keys: set[str] = set()
    high_count = 0
    medium_count = 0

    for raw in items:
        if not isinstance(raw, UnresolvedReviewItem):
            continue
        if raw.stable_id in skip_ids:
            continue
        sev = raw.inferred_severity
        if sev not in ("high", "medium"):
            continue
        high_count, medium_count = _quality_gate_bump_seen(
            seen_keys, high_count, medium_count, _quality_gate_dedupe_key_for_item(raw), sev
        )

    for finding, fp in to_post:
        sev = finding.severity
        if sev not in ("high", "medium"):
            continue
        high_count, medium_count = _quality_gate_bump_seen(
            seen_keys,
            high_count,
            medium_count,
            _quality_gate_dedupe_key_for_new_finding(finding, fp),
            sev,
        )

    return high_count, medium_count


def _compute_review_decision_from_counts(
    high_count: int,
    medium_count: int,
    *,
    high_threshold: int,
    medium_threshold: int,
) -> str:
    """Return REQUEST_CHANGES or APPROVE from aggregated open high/medium counts."""
    if high_count >= high_threshold or medium_count >= medium_threshold:
        return "REQUEST_CHANGES"
    return "APPROVE"


def _log_quality_gate_review_outcome(context: str, gate_outcome: QualityGateOutcome) -> None:
    """Emit a stable log line for the computed quality gate and derived PR decision."""
    logger.info(
        "%s quality gate: open_high=%d open_medium=%d => decision=%s",
        context,
        gate_outcome.high_count,
        gate_outcome.medium_count,
        gate_outcome.decision,
    )


def _compute_quality_gate_review_outcome(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    to_post: list[tuple[FindingV1, str]],
    cfg,
    *,
    excluded_gate_stable_ids: frozenset[str] | None = None,
) -> QualityGateOutcome | None:
    """Combine provider unresolved items with planned posts; apply thresholds; build reason text."""
    counts = _quality_gate_high_medium_counts(
        provider,
        owner,
        repo,
        pr_number,
        to_post,
        excluded_stable_ids=excluded_gate_stable_ids,
    )
    if counts is None:
        return None
    high_count, medium_count = counts
    high_threshold = int(getattr(cfg, "review_decision_high_threshold", 1))
    medium_threshold = int(getattr(cfg, "review_decision_medium_threshold", 3))
    decision = _compute_review_decision_from_counts(
        high_count,
        medium_count,
        high_threshold=high_threshold,
        medium_threshold=medium_threshold,
    )
    submission_reason = (
        f"Auto decision by Viper: aggregated open high={high_count} (threshold {high_threshold}), "
        f"open medium={medium_count} (threshold {medium_threshold}) "
        f"=> {decision}."
    )
    return QualityGateOutcome(
        high_count=high_count,
        medium_count=medium_count,
        decision=decision,
        submission_reason=submission_reason,
    )


class QualityGate:
    """Encapsulates quality gate evaluation for a pull request."""

    def __init__(self, provider, owner: str, repo: str, pr_number: int, cfg) -> None:
        self.provider = provider
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.cfg = cfg

    def evaluate(
        self,
        to_post: list[tuple[FindingV1, str]],
        excluded_gate_stable_ids: frozenset[str] | None = None,
    ) -> QualityGateOutcome | None:
        """Compute quality gate outcome from existing unresolved items and new findings."""
        return _compute_quality_gate_review_outcome(
            self.provider,
            self.owner,
            self.repo,
            self.pr_number,
            to_post,
            self.cfg,
            excluded_gate_stable_ids=excluded_gate_stable_ids,
        )
