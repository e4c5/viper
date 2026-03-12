"""CLI tests: code-review parses args and invokes runner."""

import os
from unittest.mock import patch

import pytest

# Typer raises click.exceptions.Exit
try:
    from click.exceptions import Exit as ClickExit
except ImportError:
    ClickExit = SystemExit

from code_review.__main__ import review


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
            review(owner=None, repo=None, pr=None, head_sha=None)
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


def test_cli_head_sha_required_when_not_dry_run_exits_1():
    """When dry_run is False, head_sha is required."""
    with pytest.raises(ClickExit) as exc_info:
        review(owner="o", repo="r", pr=1, head_sha="", dry_run=False)
    assert exc_info.value.exit_code == 1


def test_cli_fail_on_critical_exits_2():
    """When fail_on_critical is True and there is a critical finding, exit 2."""
    from code_review.schemas.findings import FindingV1

    with patch("code_review.__main__.run_review") as mock_run:
        mock_run.return_value = [
            FindingV1(path="x.py", line=1, severity="critical", code="x", message="Bug"),
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

    # Typer app with --help exits 0; we just ensure it doesn't raise
    try:
        app(["--help"])
    except ClickExit as e:
        assert e.exit_code == 0
    except SystemExit as e:
        assert e.code == 0


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
