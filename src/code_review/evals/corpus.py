"""Typed loaders for the checked-in evaluation corpus."""

from __future__ import annotations

import json
from importlib import resources

from pydantic import BaseModel, Field

from code_review.schemas.findings import FindingsBatchV1
from code_review.schemas.reply_dismissal import ReplyDismissalVerdictV1

_FIXTURE_PACKAGE = "code_review.evals.fixtures"


class GoldenPrReviewCase(BaseModel):
    """A checked-in golden PR review case for local and CI evaluation."""

    case_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    language: str = Field(..., min_length=1)
    pr_diff: str = Field(..., min_length=1)
    expected: FindingsBatchV1


class ReplyDismissalEvalCase(BaseModel):
    """A checked-in reply-dismissal judgment case."""

    case_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    user_message: str = Field(..., min_length=1)
    expected: ReplyDismissalVerdictV1


def _load_fixture_json(filename: str) -> object:
    text = resources.files(_FIXTURE_PACKAGE).joinpath(filename).read_text(encoding="utf-8")
    return json.loads(text)


def load_golden_pr_review_cases() -> list[GoldenPrReviewCase]:
    """Load and validate the checked-in golden PR review cases."""
    raw_cases = _load_fixture_json("golden_pr_review_cases.json")
    if not isinstance(raw_cases, list):
        raise ValueError("golden_pr_review_cases.json must contain a JSON array")
    return [GoldenPrReviewCase.model_validate(case) for case in raw_cases]


def load_reply_dismissal_eval_cases() -> list[ReplyDismissalEvalCase]:
    """Load and validate the checked-in reply-dismissal evaluation cases."""
    raw_cases = _load_fixture_json("reply_dismissal_eval_cases.json")
    if not isinstance(raw_cases, list):
        raise ValueError("reply_dismissal_eval_cases.json must contain a JSON array")
    return [ReplyDismissalEvalCase.model_validate(case) for case in raw_cases]
