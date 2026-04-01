"""Quality gate outcome value object."""

from __future__ import annotations

from dataclasses import dataclass

from code_review.providers.base import ReviewDecision


@dataclass(frozen=True)
class QualityGateOutcome:
    """Aggregated quality-gate counts and derived review decision (single source of truth)."""

    high_count: int
    medium_count: int
    decision: ReviewDecision
    submission_reason: str


# Backward-compatible alias
QualityGateReviewOutcome = QualityGateOutcome
