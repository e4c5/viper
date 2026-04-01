"""Tests for resolved status and fingerprint-based behavior (Phase 2)."""

from unittest.mock import MagicMock, patch

from code_review.diff.fingerprint import format_comment_body_with_marker
from code_review.providers.base import (
    FileInfo,
    ProviderCapabilities,
    ProviderInterface,
    ReviewComment,
)
from tests.conftest import runner_run_async_returning, sample_unified_diff


def test_review_comment_has_resolved():
    c = ReviewComment(id="1", path="foo.py", line=10, body="[High] Bug.", resolved=False)
    assert c.resolved is False
    c2 = ReviewComment(id="2", path="a.py", line=1, body="Done", resolved=True)
    assert c2.resolved is True


def test_provider_capabilities_resolvable():
    caps = ProviderCapabilities(resolvable_comments=True, supports_suggestions=False)
    assert caps.resolvable_comments is True
    caps2 = ProviderCapabilities(resolvable_comments=False, supports_suggestions=False)
    assert caps2.resolvable_comments is False


class _ProviderWithCapabilities(ProviderInterface):
    """Minimal provider for testing auto-resolve behavior via capabilities()."""

    def __init__(self, resolvable: bool):
        self._caps = ProviderCapabilities(
            resolvable_comments=resolvable,
            supports_suggestions=False,
        )
        self._resolved_ids: list[str] = []

    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    # The following methods are only what's needed by run_review in these tests.
    def get_pr_diff(  # pragma: no cover - not used directly
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> str:
        return sample_unified_diff("foo.py")

    def get_pr_diff_for_file(self, owner: str, repo: str, pr_number: int, path: str) -> str:
        return sample_unified_diff(path or "foo.py")

    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        return "content"

    def get_file_lines(self, owner, repo, ref, path, start_line, end_line) -> str:
        return "content"

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        return [FileInfo(path="foo.py", status="modified")]

    def post_review_comments(self, owner, repo, pr_number, comments, head_sha: str = "") -> None:
        return None

    def get_existing_review_comments(self, owner, repo, pr_number) -> list[ReviewComment]:
        return []

    def post_pr_summary_comment(self, owner, repo, pr_number, body: str) -> None:
        return None

    def get_pr_info(self, owner, repo, pr_number):
        return None

    def resolve_comment(self, owner: str, repo: str, comment_id: str) -> None:
        self._resolved_ids.append(comment_id)


def _run_with_single_stale_comment(
    mock_get_scm_config,
    mock_get_llm_config,
    mock_get_context_window,
    *,
    resolvable: bool,
    dry_run: bool,
):
    """Common setup for auto-resolve tests: one stale comment and empty findings."""
    from code_review.runner import run_review

    mock_get_scm_config.return_value = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
    )
    mock_get_llm_config.return_value = MagicMock(
        provider="gemini",
        model="gemini-2.5-flash",
    )
    mock_get_context_window.return_value = 1_000_000

    provider = _ProviderWithCapabilities(resolvable=resolvable)
    body_with_marker = format_comment_body_with_marker(
        "[Medium] Old issue.", fingerprint="stale-fp", version="1", run_id="run-1"
    )
    existing = [
        ReviewComment(
            id="c-1",
            path="foo.py",
            line=1,
            body=body_with_marker,
            resolved=False,
        )
    ]
    provider.get_existing_review_comments = MagicMock(return_value=existing)

    # Patch provider factory and ADK Runner so run_review sees no findings (empty array).
    with (
        patch(
            "code_review.orchestration.orchestrator.runner_mod.get_provider",
            return_value=provider,
        ),
        patch("google.adk.runners.Runner") as mock_runner_cls,
    ):
        findings_json = '{"findings":[]}'
        mock_event = MagicMock()
        mock_event.is_final_response.return_value = True
        mock_event.content = MagicMock()
        mock_event.content.parts = [MagicMock(text=findings_json)]
        mock_runner_instance = MagicMock()
        mock_runner_instance.run_async = runner_run_async_returning([mock_event])
        mock_runner_cls.return_value = mock_runner_instance

        result = run_review("o", "r", 1, head_sha="abc123", dry_run=dry_run)

    return provider, result


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_auto_resolve_stale_comments_when_capabilities_true(
    mock_get_scm_config, mock_get_llm_config, mock_get_context_window
):
    """When capabilities().resolvable_comments is True, stale comments are auto-resolved."""
    provider, result = _run_with_single_stale_comment(
        mock_get_scm_config,
        mock_get_llm_config,
        mock_get_context_window,
        resolvable=True,
        dry_run=False,
    )

    # No new findings, but the stale existing comment should be auto-resolved.
    assert result == []
    assert provider._resolved_ids == ["c-1"]


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_auto_resolve_not_called_when_capabilities_false(
    mock_get_scm_config, mock_get_llm_config, mock_get_context_window
):
    """
    When resolvable_comments is False, resolve_comment is not called
    even if comments are stale.
    """
    provider, result = _run_with_single_stale_comment(
        mock_get_scm_config,
        mock_get_llm_config,
        mock_get_context_window,
        resolvable=False,
        dry_run=False,
    )

    # No resolve_comment calls when capabilities().resolvable_comments is False.
    assert result == []
    assert provider._resolved_ids == []


@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_auto_resolve_not_called_in_dry_run(
    mock_get_scm_config, mock_get_llm_config, mock_get_context_window
):
    """Dry runs must not call resolve_comment even when resolvable_comments is True."""
    provider, result = _run_with_single_stale_comment(
        mock_get_scm_config,
        mock_get_llm_config,
        mock_get_context_window,
        resolvable=True,
        dry_run=True,
    )

    # No resolve_comment calls during dry_run.
    assert result == []
    assert provider._resolved_ids == []
