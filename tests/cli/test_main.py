"""CLI tests: code-review parses args and invokes runner."""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import typer

# Typer raises click.exceptions.Exit
try:
    from click.exceptions import Exit as ClickExit
except ImportError:
    ClickExit = SystemExit

from code_review.__main__ import review


def test_cli_ensure_logging_logs_startup_config():
    from code_review.__main__ import _ensure_logging

    with (
        patch("code_review.__main__.configure_logging") as mock_configure,
        patch("code_review.__main__.log_startup_configuration") as mock_log_startup,
    ):
        _ensure_logging()

    mock_configure.assert_called_once_with()
    mock_log_startup.assert_called_once_with()


def test_cli_missing_owner_exits_1():
    """Missing owner, repo, or pr causes exit 1."""
    with patch.dict(os.environ, {"SCM_OWNER": "", "SCM_REPO": "", "SCM_PR_NUM": ""}, clear=False):
        with pytest.raises(ClickExit) as exc_info:
            review(owner="", repo="r", pr=1, head_sha="")
        assert exc_info.value.exit_code == 1


def test_cli_missing_pr_exits_1():
    with patch.dict(os.environ, {"SCM_OWNER": "o", "SCM_REPO": "r", "SCM_PR_NUM": ""}, clear=False):
        with pytest.raises(ClickExit) as exc_info:
            review(owner="o", repo="r", pr=None, head_sha="")
        assert exc_info.value.exit_code == 1


def test_cli_invokes_run_review_with_args():
    """When owner, repo, pr are set, run_review is called with correct args."""
    with patch("code_review.__main__.run_review") as mock_run:
        mock_run.return_value = []
        review(owner="myorg", repo="myrepo", pr=42, head_sha="abc123", dry_run=True)
        mock_run.assert_called_once()
        call_kw = mock_run.call_args[1]
        assert call_kw["owner"] == "myorg"
        assert call_kw["repo"] == "myrepo"
        assert call_kw["pr_number"] == 42
        assert call_kw["head_sha"] == "abc123"
        assert call_kw["dry_run"] is True


def test_cli_uses_env_vars():
    with patch.dict(
        os.environ,
        {
            "SCM_OWNER": "env-owner",
            "SCM_REPO": "env-repo",
            "SCM_PR_NUM": "7",
            "SCM_HEAD_SHA": "sha",
        },
        clear=False,
    ):
        with patch("code_review.__main__.run_review") as mock_run:
            mock_run.return_value = []
            review(owner=None, repo=None, pr=None, head_sha="")
            call_kw = mock_run.call_args[1]
            assert call_kw["owner"] == "env-owner"
            assert call_kw["repo"] == "env-repo"
            assert call_kw["pr_number"] == 7
            assert call_kw["head_sha"] == "sha"


def test_cli_invalid_owner_repo_pattern_exits_1():
    """Owner and repo may only contain letters, digits, '_', '-', and '.'."""
    with pytest.raises(ClickExit) as exc_info:
        review(owner="org/slash", repo="r", pr=1, head_sha="abc", dry_run=True)
    assert exc_info.value.exit_code == 1
    with pytest.raises(ClickExit) as exc_info:
        review(owner="o", repo="repo@at", pr=1, head_sha="abc", dry_run=True)
    assert exc_info.value.exit_code == 1


def test_cli_review_decision_only_optioninfo_not_truthy_for_head_sha():
    """Direct ``review()`` calls must not treat Typer's default OptionInfo as decision-only."""
    with (
        patch.dict(os.environ, {"SCM_HEAD_SHA": ""}, clear=False),
        patch("code_review.__main__.get_code_review_app_config") as mock_app,
        patch("code_review.__main__.run_review") as mock_run,
    ):
        mock_app.return_value = SimpleNamespace(review_decision_only=False)
        mock_run.return_value = []
        with pytest.raises(ClickExit) as exc_info:
            review(
                owner="o",
                repo="r",
                pr=1,
                head_sha="",
                dry_run=False,
                review_decision_only=typer.Option(False, "--review-decision-only"),
            )
        assert exc_info.value.exit_code == 1
        mock_run.assert_not_called()


def test_cli_head_sha_required_when_not_dry_run_exits_1():
    """When dry_run is False, head_sha is required."""
    with (
        patch.dict(os.environ, {"SCM_HEAD_SHA": ""}, clear=False),
        patch("code_review.__main__.get_code_review_app_config") as mock_app,
        patch("code_review.__main__.run_review") as mock_run,
    ):
        mock_app.return_value = SimpleNamespace(review_decision_only=False)
        mock_run.return_value = []
        with pytest.raises(ClickExit) as exc_info:
            review(
                owner="o",
                repo="r",
                pr=1,
                head_sha="",
                dry_run=False,
                review_decision_only=False,
            )
        assert exc_info.value.exit_code == 1
        mock_run.assert_not_called()


def test_cli_allows_missing_head_sha_with_review_decision_only():
    from code_review.config import reset_config_cache

    with (
        patch.dict(os.environ, {"SCM_HEAD_SHA": ""}, clear=False),
        patch("code_review.__main__.run_review") as mock_run,
    ):
        mock_run.return_value = []
        reset_config_cache()
        try:
            review(
                owner="o",
                repo="r",
                pr=1,
                head_sha="",
                dry_run=False,
                review_decision_only=True,
            )
        finally:
            reset_config_cache()
        kw = mock_run.call_args[1]
        assert kw["review_decision_only"] is True
        assert kw["head_sha"] == ""


def test_cli_fail_on_critical_exits_2():
    """When fail_on_critical is True and there is a critical finding, exit 2."""
    from code_review.schemas.findings import FindingV1

    with patch("code_review.__main__.run_review") as mock_run:
        mock_run.return_value = [
            FindingV1(path="x.py", line=1, severity="high", code="x", message="Bug"),
        ]
        with pytest.raises(ClickExit) as exc_info:
            review(
                owner="o",
                repo="r",
                pr=1,
                head_sha="abc123",
                dry_run=True,
                fail_on_critical=True,
            )
        assert exc_info.value.exit_code == 2


def test_cli_app_invokable_as_main():
    """app() can be invoked (e.g. when run as __main__) and responds to --help."""
    from code_review.__main__ import app

    with pytest.raises((ClickExit, SystemExit)) as exc_info:
        app(["--help"])
    ex = exc_info.value
    code = getattr(ex, "exit_code", None)
    if code is None:
        code = getattr(ex, "code", None)
    assert code == 0


def test_cli_review_decision_options_passed_to_run_review():
    with patch("code_review.__main__.run_review") as mock_run:
        mock_run.return_value = []
        review(
            owner="o",
            repo="r",
            pr=1,
            head_sha="abc123",
            dry_run=True,
            review_decision_enabled=True,
            review_decision_high_threshold=4,
            review_decision_medium_threshold=6,
        )
        mock_run.assert_called_once()
        kw = mock_run.call_args[1]
        assert kw["review_decision_enabled"] is True
        assert kw["review_decision_high_threshold"] == 4
        assert kw["review_decision_medium_threshold"] == 6


def test_cli_main_module_entry_point():
    """Running the package as __main__ (python -m code_review --help) invokes app()."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "code_review", "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert "Run the code review agent" in result.stdout or "review" in result.stdout
