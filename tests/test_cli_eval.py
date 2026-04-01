"""Tests for the local eval CLI command."""

from unittest.mock import patch

from typer.testing import CliRunner

from code_review.evals.cli import app


def test_eval_cli_runs_all_suites() -> None:
    result = CliRunner().invoke(app, [])

    assert result.exit_code == 0
    assert "golden_pr_review[parser]:" in result.stdout
    assert "reply_dismissal[parser]:" in result.stdout
    assert "[PASS]" in result.stdout


def test_eval_cli_rejects_unknown_suite() -> None:
    result = CliRunner().invoke(app, ["--suite", "unknown"])

    assert result.exit_code == 2
    assert "Unknown eval suite" in result.stderr


def test_eval_cli_rejects_unknown_execution_mode() -> None:
    result = CliRunner().invoke(app, ["--execution", "weird"])

    assert result.exit_code == 2
    assert "Unknown eval execution mode" in result.stderr


@patch("code_review.evals.cli.run_local_reply_dismissal_eval")
@patch("code_review.evals.cli.run_local_golden_pr_review_eval")
def test_eval_cli_passes_execution_mode_to_harnesses(
    mock_golden_eval, mock_reply_eval
) -> None:
    mock_golden_eval.return_value = type(
        "Summary",
        (),
        {
            "suite_name": "golden_pr_review[adk]",
            "passed": 1,
            "total": 1,
            "failed": 0,
            "results": [],
        },
    )()
    mock_reply_eval.return_value = type(
        "Summary",
        (),
        {
            "suite_name": "reply_dismissal[adk]",
            "passed": 1,
            "total": 1,
            "failed": 0,
            "results": [],
        },
    )()

    result = CliRunner().invoke(app, ["--execution", "adk"])

    assert result.exit_code == 0
    mock_golden_eval.assert_called_once_with(execution="adk")
    mock_reply_eval.assert_called_once_with(execution="adk")
