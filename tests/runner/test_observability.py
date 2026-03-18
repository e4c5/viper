"""Tests for run_review observability: trace_id and structured run_complete log (Phase 4.3)."""

from unittest.mock import MagicMock, patch

from tests.conftest import runner_run_async_returning
from code_review.providers.base import FileInfo


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_emits_trace_id_and_run_complete(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
):
    """run_review logs run_complete with trace_id, owner, repo, pr_number, counts, duration_ms."""
    from code_review.runner import run_review

    mock_scm.return_value = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="",
        skip_title_pattern="",
    )
    mock_llm.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    provider = MagicMock()
    provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    provider.get_pr_diff.return_value = "diff"
    provider.get_file_content.return_value = "content"
    provider.get_existing_review_comments.return_value = []
    provider.post_review_comments = MagicMock()
    provider.post_pr_summary_comment = MagicMock()
    mock_get_provider.return_value = provider
    mock_context_window.return_value = 1_000_000

    findings_json = (
        '[{"path":"foo.py","line":1,"severity":"medium","code":"x",'
        '"message":"Fix."}]'
    )
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=findings_json)]
    mock_runner_instance = MagicMock()
    mock_runner_instance.run_async = runner_run_async_returning([mock_event])

    run_complete_calls = []

    def capture_run_complete(
        trace_id, owner, repo, pr_number, files_count, findings_count, posts_count, duration_ms
    ):
        run_complete_calls.append(
            (trace_id, owner, repo, pr_number, files_count, findings_count, posts_count, duration_ms)
        )

    with patch("code_review.runner._log_run_complete", side_effect=capture_run_complete):
        with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
            run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    assert len(run_complete_calls) == 1
    (
        trace_id,
        owner,
        repo,
        pr_number,
        files_count,
        findings_count,
        posts_count,
        duration_ms,
    ) = run_complete_calls[0]
    assert trace_id is not None
    assert len(trace_id) == 36  # UUID string length
    assert owner == "o"
    assert repo == "r"
    assert pr_number == 1
    assert files_count == 1
    assert findings_count == 1
    assert posts_count == 1
    assert duration_ms is not None


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_emits_run_complete_on_early_exit(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
):
    """When run is skipped (e.g. skip label), run_complete is still logged with 0 counts."""
    from code_review.providers.base import PRInfo
    from code_review.runner import run_review

    mock_scm.return_value = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="skip-review",
        skip_title_pattern="",
    )
    mock_llm.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    provider = MagicMock()
    provider.get_pr_info.return_value = PRInfo(title="WIP", labels=["skip-review"])
    mock_get_provider.return_value = provider
    mock_context_window.return_value = 1_000_000

    run_complete_calls = []

    def capture_run_complete(
        trace_id, owner, repo, pr_number, files_count, findings_count, posts_count, duration_ms
    ):
        run_complete_calls.append(
            (trace_id, owner, repo, pr_number, files_count, findings_count, posts_count, duration_ms)
        )

    with patch("code_review.runner._log_run_complete", side_effect=capture_run_complete):
        result = run_review("o", "r", 1, head_sha="abc", dry_run=False)

    assert result == []
    assert len(run_complete_calls) == 1
    (
        trace_id,
        _owner,
        _repo,
        _pr_number,
        files_count,
        _findings_count,
        posts_count,
        _duration_ms,
    ) = run_complete_calls[0]
    assert trace_id is not None
    assert posts_count == 0
    assert files_count == 0
