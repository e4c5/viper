"""Large PR fixture: validate chunking and no duplicate posts across file-by-file runs (Phase 5)."""

from unittest.mock import MagicMock, patch

from code_review.providers.base import FileInfo


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_large_pr_file_by_file_no_duplicate_posts(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
):
    """When diff exceeds token budget, runner invokes agent per file; posted comments have no duplicate (path, line)."""
    from code_review.runner import run_review

    mock_scm.return_value = MagicMock(
        provider="gitea", url="https://x.com", token="x",
        skip_label="", skip_title_pattern="",
    )
    mock_llm.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    provider = MagicMock()
    provider.get_pr_files.return_value = [
        FileInfo(path="a.py", status="modified"),
        FileInfo(path="b.py", status="modified"),
    ]
    # Large diff so use_file_by_file is True (budget = 0.25 * 100 = 25 tokens, diff has > 100 chars)
    provider.get_pr_diff.return_value = "x" * 200
    provider.get_file_content.return_value = "line1\nline2\n"
    provider.get_existing_review_comments.return_value = []
    provider.post_review_comments = MagicMock()
    provider.post_pr_summary_comment = MagicMock()
    mock_get_provider.return_value = provider
    mock_context_window.return_value = 100  # small so diff is "over budget"

    run_calls = []

    def capture_run(*, new_message, **kwargs):
        run_calls.append(new_message)
        # Return one finding for the file mentioned in the message
        text = new_message.parts[0].text if new_message.parts else ""
        if "Review only this file: a.py" in text:
            findings = '[{"path":"a.py","line":1,"severity":"suggestion","code":"x","message":"Fix a."}]'
        elif "Review only this file: b.py" in text:
            findings = '[{"path":"b.py","line":2,"severity":"info","code":"y","message":"Fix b."}]'
        else:
            findings = "[]"
        mock_event = MagicMock()
        mock_event.is_final_response.return_value = True
        mock_event.content = MagicMock()
        mock_event.content.parts = [MagicMock(text=findings)]
        return iter([mock_event])

    mock_runner_instance = MagicMock()
    mock_runner_instance.run.side_effect = capture_run

    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    # File-by-file: two agent runs (one per file)
    assert len(run_calls) == 2
    provider.post_review_comments.assert_called_once()
    comments = provider.post_review_comments.call_args[0][3]
    assert len(comments) == 2
    path_lines = [(c.path, c.line) for c in comments]
    assert len(path_lines) == len(set(path_lines)), "expected no duplicate (path, line)"
    paths = {c.path for c in comments}
    assert paths == {"a.py", "b.py"}
