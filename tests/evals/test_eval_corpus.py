"""Tests for the checked-in evaluation corpus loaders."""

from code_review.evals import (
    load_golden_pr_review_cases,
    load_reply_dismissal_eval_cases,
)


def test_load_golden_pr_review_cases_returns_valid_cases() -> None:
    cases = load_golden_pr_review_cases()

    assert len(cases) >= 2
    assert {case.case_id for case in cases} >= {
        "python_missing_none_guard",
        "java_sql_concatenation",
    }
    for case in cases:
        assert case.pr_diff.startswith("diff --git ")
        assert isinstance(case.expected.findings, list)


def test_load_reply_dismissal_eval_cases_returns_valid_cases() -> None:
    cases = load_reply_dismissal_eval_cases()

    assert len(cases) >= 2
    assert {case.case_id for case in cases} >= {
        "agreed_when_thread_is_clearly_addressed",
        "disagreed_when_reply_only_promises_future_work",
    }
    for case in cases:
        assert "Original automated review comment id" in case.user_message
        assert case.expected.verdict in {"agreed", "disagreed"}


def test_disagreed_reply_dismissal_cases_require_reply_text() -> None:
    disagreed_cases = [
        case for case in load_reply_dismissal_eval_cases() if case.expected.verdict == "disagreed"
    ]

    assert disagreed_cases
    assert all(case.expected.reply_text.strip() for case in disagreed_cases)
