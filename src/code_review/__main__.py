"""CLI entry point for the code review agent."""

import os
import re

import typer
from typer.models import OptionInfo

from code_review.config import get_code_review_app_config, log_startup_configuration
from code_review.logging_config import configure_logging
from code_review.runner import run_review


def _ensure_logging() -> None:
    """Configure logging from CODE_REVIEW_LOG_LEVEL before running."""
    configure_logging()
    log_startup_configuration()


app = typer.Typer()


OWNER_REPO_PATTERN = r"^[a-zA-Z0-9_.-]+$"


def _typer_bool(value: bool | object, *, default: bool = False) -> bool:
    """Coerce Typer defaults to bool when ``review()`` is called outside Typer (no injection)."""
    if isinstance(value, OptionInfo):
        return default
    return bool(value)


def _normalize_review_decision_options(
    review_decision_enabled: bool | None,
    review_decision_high_threshold: int | None,
    review_decision_medium_threshold: int | None,
) -> tuple[bool | None, int | None, int | None]:
    if isinstance(review_decision_enabled, OptionInfo):
        review_decision_enabled = None
    if isinstance(review_decision_high_threshold, OptionInfo):
        review_decision_high_threshold = None
    if isinstance(review_decision_medium_threshold, OptionInfo):
        review_decision_medium_threshold = None
    return review_decision_enabled, review_decision_high_threshold, review_decision_medium_threshold


def _cli_resolve_owner_repo_pr(
    owner: str | None,
    repo: str | None,
    pr: int | None,
    head_sha: str,
) -> tuple[str, str, int | None, str]:
    owner_f = owner or os.environ.get("SCM_OWNER", "")
    repo_f = repo or os.environ.get("SCM_REPO", "")
    pr_num = pr if pr is not None else _parse_int(os.environ.get("SCM_PR_NUM", ""))
    head_sha_val = head_sha or os.environ.get("SCM_HEAD_SHA", "")
    return owner_f, repo_f, pr_num, head_sha_val


def _cli_validate_inputs(
    owner: str,
    repo: str,
    pr_num: int | None,
    head_sha_val: str,
    dry_run: bool,
    *,
    review_decision_only: bool = False,
) -> None:
    if not owner or not repo or pr_num is None:
        typer.echo(
            "Error: owner, repo, and pr are required (--owner, --repo, --pr or SCM_* env vars)",
            err=True,
        )
        raise typer.Exit(1)

    if not re.match(OWNER_REPO_PATTERN, owner) or not re.match(OWNER_REPO_PATTERN, repo):
        typer.echo(
            "Error: owner and repo may only contain letters, digits, '_', '-', and '.'.",
            err=True,
        )
        raise typer.Exit(1)

    decision_only_effective = bool(review_decision_only) or bool(
        get_code_review_app_config().review_decision_only
    )
    if not dry_run and not head_sha_val and not decision_only_effective:
        typer.echo(
            "Error: head_sha is required when posting comments (dry_run=False). "
            "Provide --head-sha or SCM_HEAD_SHA, use --review-decision-only "
            "(or CODE_REVIEW_REVIEW_DECISION_ONLY=1; head resolved via API when omitted), "
            "or use --dry-run.",
            err=True,
        )
        raise typer.Exit(1)


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
    review_decision_only: bool = typer.Option(
        False,
        "--review-decision-only",
        help=(
            "Skip LLM and inline comments; only recompute quality gate and "
            "submit PR review decision (also set CODE_REVIEW_REVIEW_DECISION_ONLY=1). "
            "head_sha optional — fetched from API if missing."
        ),
    ),
) -> None:
    """Run the code review agent on a pull request."""
    review_decision_enabled, review_decision_high_threshold, review_decision_medium_threshold = (
        _normalize_review_decision_options(
            review_decision_enabled,
            review_decision_high_threshold,
            review_decision_medium_threshold,
        )
    )
    dry_run = _typer_bool(dry_run)
    print_findings = _typer_bool(print_findings)
    fail_on_critical = _typer_bool(fail_on_critical)
    review_decision_only = _typer_bool(review_decision_only)

    owner_f, repo_f, pr_num, head_sha_val = _cli_resolve_owner_repo_pr(owner, repo, pr, head_sha)
    _cli_validate_inputs(
        owner_f, repo_f, pr_num, head_sha_val, dry_run, review_decision_only=review_decision_only
    )

    _ensure_logging()
    findings = run_review(
        owner=owner_f,
        repo=repo_f,
        pr_number=pr_num,
        head_sha=head_sha_val,
        dry_run=dry_run,
        print_findings=print_findings,
        review_decision_enabled=review_decision_enabled,
        review_decision_high_threshold=review_decision_high_threshold,
        review_decision_medium_threshold=review_decision_medium_threshold,
        review_decision_only=review_decision_only,
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
