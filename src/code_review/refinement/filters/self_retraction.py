"""Self-retraction finding filter."""

from __future__ import annotations

import logging
import re

from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)

_SELF_RETRACTION_MESSAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:i\s+will|i['\u2019]ll)\s+retract\b", re.I),
    re.compile(r"\bi\s+retract\b", re.I),
    re.compile(r"\bretract(?:ed|ing)?\s+this\s+finding\b", re.I),
    re.compile(r"\bretract\s+this\b", re.I),
    re.compile(r"\b(?:this|that|it)\s+is\s+(?:a\s+)?false\s+positive\b", re.I),
    re.compile(r"\bthat\s+was\s+(?:a\s+)?false\s+positive\b", re.I),
    re.compile(r"\bit's\s+(?:a\s+)?false\s+positive\b", re.I),
    re.compile(r"\bwithdraw\s+this\s+finding\b", re.I),
    re.compile(r"\bdisregard\s+this\b", re.I),
    re.compile(r"\bignore\s+this\s+(?:finding|comment)\b", re.I),
    re.compile(r"\b(?:i|we)\s+no\s+longer\s+(?:believe|think)\b", re.I),
    re.compile(r"\bi\s+was\s+wrong\b", re.I),
    re.compile(r"\bi\s+was\s+mistaken\b", re.I),
    re.compile(r"\bmy\s+mistake\b", re.I),
    re.compile(r"\bon\s+second\s+thought\b", re.I),
    re.compile(r"\bupon\s+reflection\b", re.I),
    re.compile(r"\b(?:sorry|apologies),?\s+(?:ignore|disregard)\b", re.I),
    re.compile(r"\bactually,?\s+this\s+is\s+(?:fine|correct|acceptable)\b", re.I),
    re.compile(r"\bhowever,?\s+this\s+is\s+(?:fine|correct|acceptable)\b", re.I),
)


def _finding_message_looks_self_retracted(message: str) -> bool:
    """True when the model walked back or disowned the issue inside the same message."""
    if not message or not str(message).strip():
        return False
    return any(p.search(message) for p in _SELF_RETRACTION_MESSAGE_PATTERNS)


def filter_self_retracted_findings(findings: list[FindingV1]) -> list[FindingV1]:
    """Drop findings whose message text retracts or negates the issue (non-actionable noise)."""
    if not findings:
        return findings
    kept: list[FindingV1] = []
    for f in findings:
        if _finding_message_looks_self_retracted(f.message):
            logger.info(
                "Dropping finding with self-retracted or withdrawn message text: %s:%d",
                f.path,
                f.line,
            )
            continue
        kept.append(f)
    return kept
