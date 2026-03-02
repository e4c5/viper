"""Tests for run_review observability: trace_id and structured run_complete log (Phase 4.3)."""

import logging
from unittest.mock import MagicMock, patch

from code_review.providers.base import FileInfo


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_emits_trace_id_and_run_complete(
    mock_scm, mock_get_provider, mock_llm, mock_context_window, caplog
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
        '[{"path":"foo.py","line":1,"severity":"suggestion","code":"x",'
        '"message":"Fix."}]'
    )
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=findings_json)]
    mock_runner_instance = MagicMock()
    mock_runner_instance.run.return_value = iter([mock_event])

    with caplog.at_level(logging.INFO):
        with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
            run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    run_complete_records = [r for r in caplog.records if r.getMessage() == "run_complete"]
    assert len(run_complete_records) == 1
    rec = run_complete_records[0]
    assert getattr(rec, "trace_id", None) is not None
    assert len(getattr(rec, "trace_id", "")) == 36  # UUID string length
    assert getattr(rec, "owner", None) == "o"
    assert getattr(rec, "repo", None) == "r"
    assert getattr(rec, "pr_number", None) == 1
    assert getattr(rec, "files_count", None) == 1
    assert getattr(rec, "findings_count", None) == 1
    assert getattr(rec, "posts_count", None) == 1
    assert getattr(rec, "duration_ms", None) is not None


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_emits_run_complete_on_early_exit(
    mock_scm, mock_get_provider, mock_llm, mock_context_window, caplog
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

    with caplog.at_level(logging.INFO):
        result = run_review("o", "r", 1, head_sha="abc", dry_run=False)

    assert result == []
    run_complete_records = [r for r in caplog.records if r.getMessage() == "run_complete"]
    assert len(run_complete_records) == 1
    rec = run_complete_records[0]
    assert getattr(rec, "trace_id", None) is not None
    assert getattr(rec, "posts_count", None) == 0
    assert getattr(rec, "files_count", None) == 0
