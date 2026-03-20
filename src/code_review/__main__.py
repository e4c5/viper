"""CLI entry point for the code review agent."""

import os

import typer
from typer.models import OptionInfo

from code_review.logging_config import configure_logging
from code_review.runner import run_review


def _ensure_logging() -> None:
    """Configure logging from CODE_REVIEW_LOG_LEVEL before running."""
    configure_logging()

app = typer.Typer()


OWNER_REPO_PATTERN = r"^[a-zA-Z0-9_.-]+$"


@app.command()
def review(
    owner: str | None = typer.Option(
        None,
        "--owner",
        "-o",
        help="Repo owner (or set SCM_OWNER)",
    ),
    repo: str | None = typer.Option(
        None,
        "--repo",
        "-r",
        help="Repo name (or set SCM_REPO)",
    ),
    pr: int | None = typer.Option(
        None,
        "--pr",
        "-p",
        help="PR number (or set SCM_PR_NUM)",
    ),
    head_sha: str = typer.Option(
        "",
        "--head-sha",
        help="Head commit SHA (or set SCM_HEAD_SHA)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Parse and filter findings but do not post comments",
    ),
    print_findings: bool = typer.Option(
        False,
        "--print-findings",
        help="Print each finding to stdout (path:line [severity] message)",
    ),
    fail_on_critical: bool = typer.Option(
        False,
        "--fail-on-critical",
        help="Exit with non-zero status if any finding is high severity",
    ),
    review_decision_enabled: bool | None = typer.Option(
        None,
        "--review-decision-enabled/--no-review-decision-enabled",
        help="Enable/disable automatic PR review decision submission.",
    ),
    review_decision_high_threshold: int | None = typer.Option(
        None,
        "--review-decision-high-threshold",
        min=0,
        help="Request changes when open high findings reach this threshold.",
    ),
    review_decision_medium_threshold: int | None = typer.Option(
        None,
        "--review-decision-medium-threshold",
        min=0,
        help="Request changes when open medium findings reach this threshold.",
    ),
) -> None:
    """Run the code review agent on a pull request."""
    if isinstance(review_decision_enabled, OptionInfo):
        review_decision_enabled = None
    if isinstance(review_decision_high_threshold, OptionInfo):
        review_decision_high_threshold = None
    if isinstance(review_decision_medium_threshold, OptionInfo):
        review_decision_medium_threshold = None

    owner = owner or os.environ.get("SCM_OWNER", "")
    repo = repo or os.environ.get("SCM_REPO", "")
    pr_num = pr if pr is not None else _parse_int(os.environ.get("SCM_PR_NUM", ""))
    head_sha_val = head_sha or os.environ.get("SCM_HEAD_SHA", "")

    if not owner or not repo or pr_num is None:
        typer.echo(
            "Error: owner, repo, and pr are required (--owner, --repo, --pr or SCM_* env vars)",
            err=True,
        )
        raise typer.Exit(1)
    import re as _re

    if not _re.match(OWNER_REPO_PATTERN, owner) or not _re.match(OWNER_REPO_PATTERN, repo):
        typer.echo(
            "Error: owner and repo may only contain letters, digits, '_', '-', and '.'.",
            err=True,
        )
        raise typer.Exit(1)

    if not dry_run and not head_sha_val:
        typer.echo(
            "Error: head_sha is required when posting comments (dry_run=False). "
            "Provide --head-sha or SCM_HEAD_SHA, or use --dry-run to run without posting.",
            err=True,
        )
        raise typer.Exit(1)

    if review_decision_enabled is not None:
        os.environ["SCM_REVIEW_DECISION_ENABLED"] = (
            "true" if review_decision_enabled else "false"
        )
    if review_decision_high_threshold is not None:
        os.environ["SCM_REVIEW_DECISION_HIGH_THRESHOLD"] = str(
            review_decision_high_threshold
        )
    if review_decision_medium_threshold is not None:
        os.environ["SCM_REVIEW_DECISION_MEDIUM_THRESHOLD"] = str(
            review_decision_medium_threshold
        )

    _ensure_logging()
    findings = run_review(
        owner=owner,
        repo=repo,
        pr_number=pr_num,
        head_sha=head_sha_val,
        dry_run=dry_run,
        print_findings=print_findings,
    )
    if fail_on_critical and any(f.severity == "high" for f in findings):
        raise typer.Exit(2)


def _parse_int(s: str) -> int | None:
    try:
        return int(s) if s else None
    except ValueError:
        return None


if __name__ == "__main__":
    app()
