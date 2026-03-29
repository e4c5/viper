"""CLI entry point for local checked-in evaluation runs."""

import typer

from code_review.evals import run_local_golden_pr_review_eval, run_local_reply_dismissal_eval
from code_review.logging_config import configure_logging

app = typer.Typer()


def _ensure_logging() -> None:
    """Configure logging from CODE_REVIEW_LOG_LEVEL before running."""
    configure_logging()


@app.command()
def eval(
    suite: str = typer.Option(
        "all",
        "--suite",
        help="Which local eval suite to run: golden_pr_review, reply_dismissal, or all.",
    ),
    execution: str = typer.Option(
        "parser",
        "--execution",
        help="Eval execution mode: parser or adk.",
    ),
) -> None:
    """Run the local checked-in evaluation corpus."""
    _ensure_logging()
    if execution not in {"parser", "adk"}:
        typer.echo(f"Unknown eval execution mode: {execution}", err=True)
        raise typer.Exit(2)

    summaries = []
    if suite in {"golden_pr_review", "all"}:
        summaries.append(run_local_golden_pr_review_eval(execution=execution))
    if suite in {"reply_dismissal", "all"}:
        summaries.append(run_local_reply_dismissal_eval(execution=execution))
    if not summaries:
        typer.echo(f"Unknown eval suite: {suite}", err=True)
        raise typer.Exit(2)

    total_failed = 0
    for summary in summaries:
        typer.echo(
            f"{summary.suite_name}: {summary.passed}/{summary.total} passed, {summary.failed} failed"
        )
        for result in summary.results:
            status = "PASS" if result.passed else "FAIL"
            typer.echo(f"  [{status}] {result.case_id}: {result.detail}")
        total_failed += summary.failed

    if total_failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
