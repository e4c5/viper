"""Regression guard for the checked-in parser eval baseline."""

import json
from pathlib import Path

from code_review.evals import run_local_golden_pr_review_eval, run_local_reply_dismissal_eval


def _summary_to_dict(summary) -> dict:
    return {
        "suite_name": summary.suite_name,
        "total": summary.total,
        "passed": summary.passed,
        "failed": summary.failed,
        "results": [
            {
                "case_id": result.case_id,
                "passed": result.passed,
                "detail": result.detail,
            }
            for result in summary.results
        ],
    }


def test_parser_eval_baseline_matches_checked_in_snapshot() -> None:
    baseline_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "code_review"
        / "evals"
        / "fixtures"
        / "parser_eval_baseline.json"
    )
    baseline = json.loads(baseline_path.read_text())

    current = {
        "execution": "parser",
        "suites": [
            _summary_to_dict(run_local_golden_pr_review_eval(execution="parser")),
            _summary_to_dict(run_local_reply_dismissal_eval(execution="parser")),
        ],
    }

    assert current["execution"] == baseline["execution"]
    assert current["suites"] == baseline["suites"]
