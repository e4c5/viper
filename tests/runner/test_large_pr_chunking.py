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
    """
    When diff exceeds the token budget, runner invokes the agent per file;
    posted comments have no duplicate (path, line).
    """
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
            findings = (
                '[{"path":"a.py","line":1,"severity":"suggestion","code":"x",'
                '"message":"Fix a."}]'
            )
        elif "Review only this file: b.py" in text:
            findings = (
                '[{"path":"b.py","line":2,"severity":"info","code":"y",'
                '"message":"Fix b."}]'
            )
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


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_large_pr_file_by_file_uses_separate_sessions(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
):
    """File-by-file mode uses a fresh session per file to avoid accumulating prior-file
    context in the ADK session history (which would grow the context window and waste tokens)."""
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
    provider.get_pr_files.return_value = [
        FileInfo(path="a.py", status="modified"),
        FileInfo(path="b.py", status="modified"),
    ]
    provider.get_pr_diff.return_value = "x" * 200
    provider.get_file_content.return_value = ""
    provider.get_existing_review_comments.return_value = []
    provider.post_review_comments = MagicMock()
    provider.post_pr_summary_comment = MagicMock()
    mock_get_provider.return_value = provider
    mock_context_window.return_value = 100

    session_ids_used = []

    def capture_run(*, session_id, new_message, **kwargs):
        session_ids_used.append(session_id)
        mock_event = MagicMock()
        mock_event.is_final_response.return_value = True
        mock_event.content = MagicMock()
        mock_event.content.parts = [MagicMock(text="[]")]
        return iter([mock_event])

    mock_runner_instance = MagicMock()
    mock_runner_instance.run.side_effect = capture_run

    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        run_review("o", "r", 1, head_sha="abc123", dry_run=True)

    # Each file must use a distinct session_id to avoid context bleed between files
    assert len(session_ids_used) == 2
    assert session_ids_used[0] != session_ids_used[1], (
        "file-by-file mode must use a separate ADK session per file to avoid "
        "accumulating prior-file context in the session history"
    )


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_large_pr_file_by_file_message_requests_file_diff(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
):
    """In file-by-file mode, user message explicitly asks agent to use get_pr_diff_for_file."""
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
    provider.get_pr_files.return_value = [FileInfo(path="a.py", status="modified")]
    provider.get_pr_diff.return_value = "x" * 200
    provider.get_file_content.return_value = ""
    provider.get_existing_review_comments.return_value = []
    provider.post_review_comments = MagicMock()
    provider.post_pr_summary_comment = MagicMock()
    mock_get_provider.return_value = provider
    mock_context_window.return_value = 100

    messages_sent = []

    def capture_run(*, new_message, **kwargs):
        messages_sent.append(new_message.parts[0].text if new_message.parts else "")
        mock_event = MagicMock()
        mock_event.is_final_response.return_value = True
        mock_event.content = MagicMock()
        mock_event.content.parts = [MagicMock(text="[]")]
        return iter([mock_event])

    mock_runner_instance = MagicMock()
    mock_runner_instance.run.side_effect = capture_run

    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        run_review("o", "r", 1, head_sha="sha1", dry_run=True)

    assert len(messages_sent) == 1
    msg = messages_sent[0]
    assert (
        "get_pr_diff_for_file" in msg
    ), "message to agent should instruct use of get_pr_diff_for_file in file-by-file mode"
    assert (
        "a.py" in msg
    ), "message to agent should include the file path so the agent knows which file to review"
