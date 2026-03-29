"""Large PR fixture: validate chunking and no duplicate posts across file-by-file runs (Phase 5)."""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from code_review.providers.base import FileInfo, ProviderCapabilities
from tests.conftest import runner_run_async_returning


def _configure_common_runner_mocks(
    mock_scm,
    mock_get_provider,
    mock_llm,
    mock_context_window,
    *,
    provider,
    context_window,
    scm_overrides=None,
):
    scm_config = {
        "provider": "gitea",
        "url": "https://x.com",
        "token": "x",
        "skip_label": "",
        "skip_title_pattern": "",
    }
    if scm_overrides:
        scm_config.update(scm_overrides)

    mock_scm.return_value = MagicMock(**scm_config)
    mock_llm.return_value = MagicMock(
        provider="gemini",
        model="gemini-2.5-flash",
        disable_tool_calls=False,
    )
    mock_get_provider.return_value = provider
    mock_context_window.return_value = context_window


def _make_provider(
    *,
    pr_files=None,
    pr_diff=None,
    file_content="",
    incremental_pr_files=None,
    incremental_pr_diff=None,
    capabilities=None,
):
    provider = MagicMock()
    if pr_files is not None:
        provider.get_pr_files.return_value = pr_files
    if pr_diff is not None:
        provider.get_pr_diff.return_value = pr_diff
    if incremental_pr_files is not None:
        provider.get_incremental_pr_files.return_value = incremental_pr_files
    if incremental_pr_diff is not None:
        provider.get_incremental_pr_diff.return_value = incremental_pr_diff
    if capabilities is not None:
        provider.capabilities.return_value = capabilities
    provider.get_file_content.return_value = file_content
    provider.get_existing_review_comments.return_value = []
    provider.post_review_comments = MagicMock()
    provider.post_pr_summary_comment = MagicMock()
    return provider


def _make_final_event(text="[]"):
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=text)]
    return mock_event


def _make_runner(capture_run):
    runner = MagicMock()
    runner.run_async = capture_run
    return runner


def _capture_create_review_agent(agents_created):
    def capture_create_review_agent(
        provider,
        standards="",
        findings_only=True,
        *,
        disable_tools=False,
        context_brief_attached=False,
    ):
        agents_created.append({"disable_tools": disable_tools})
        mock_agent = MagicMock()
        mock_agent.name = "code_review_agent"
        mock_agent.instruction = "..."
        mock_agent.tools = []
        return mock_agent

    return capture_create_review_agent


def _invoke_run_review(
    run_review,
    runner_instance,
    *args,
    create_review_agent_side_effect=None,
    **kwargs,
):
    with ExitStack() as stack:
        if create_review_agent_side_effect is not None:
            stack.enter_context(
                patch(
                    "code_review.orchestration_deps.create_review_agent",
                    side_effect=create_review_agent_side_effect,
                )
            )
        stack.enter_context(patch("google.adk.runners.Runner", return_value=runner_instance))
        run_review(*args, **kwargs)


@patch("code_review.orchestration_deps.get_context_window")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_scm_config")
def test_large_pr_file_by_file_no_duplicate_posts(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
):
    """
    When diff exceeds the token budget, runner invokes the agent per file;
    posted comments have no duplicate (path, line).
    """
    from code_review.runner import run_review

    provider = _make_provider(
        pr_files=[
            FileInfo(path="a.py", status="modified"),
            FileInfo(path="b.py", status="modified"),
        ],
        pr_diff="x" * 200,
        file_content="line1\nline2\n",
    )
    _configure_common_runner_mocks(
        mock_scm,
        mock_get_provider,
        mock_llm,
        mock_context_window,
        provider=provider,
        context_window=100,
    )

    run_calls = []

    def capture_run(*, new_message, **kwargs):
        run_calls.append(new_message)
        # Return one finding for the file mentioned in the message (runner uses
        # "Review exactly one file..." and get_pr_diff_for_file(..., "path") / Use path "path")
        text = new_message.parts[0].text if new_message.parts else ""
        if '"a.py"' in text:
            findings = (
                '[{"path":"a.py","line":1,"severity":"medium","code":"x","message":"Fix a."}]'
            )
        elif '"b.py"' in text:
            findings = '[{"path":"b.py","line":2,"severity":"low","code":"y","message":"Fix b."}]'
        else:
            findings = "[]"
        return runner_run_async_returning([_make_final_event(findings)])()

    _invoke_run_review(
        run_review,
        _make_runner(capture_run),
        "o",
        "r",
        1,
        head_sha="abc123",
        dry_run=False,
    )

    # File-by-file: two agent runs (one per file)
    assert len(run_calls) == 2
    # One comment per finding (no batch call)
    assert provider.post_review_comments.call_count == 2
    all_comments = [call[0][3][0] for call in provider.post_review_comments.call_args_list]
    assert len(all_comments) == 2
    path_lines = [(c.path, c.line) for c in all_comments]
    assert len(path_lines) == len(set(path_lines)), "expected no duplicate (path, line)"
    paths = {c.path for c in all_comments}
    assert paths == {"a.py", "b.py"}


@patch("code_review.orchestration_deps.get_context_window")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_scm_config")
def test_large_pr_file_by_file_uses_separate_sessions(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
):
    """File-by-file mode uses a fresh session per file to avoid accumulating prior-file
    context in the ADK session history (which would grow the context window and waste tokens)."""
    from code_review.runner import run_review

    provider = _make_provider(
        pr_files=[
            FileInfo(path="a.py", status="modified"),
            FileInfo(path="b.py", status="modified"),
        ],
        pr_diff="x" * 200,
    )
    _configure_common_runner_mocks(
        mock_scm,
        mock_get_provider,
        mock_llm,
        mock_context_window,
        provider=provider,
        context_window=100,
    )

    session_ids_used = []

    def capture_run(*, session_id, new_message, **kwargs):
        session_ids_used.append(session_id)
        return runner_run_async_returning([_make_final_event()])()

    _invoke_run_review(
        run_review,
        _make_runner(capture_run),
        "o",
        "r",
        1,
        head_sha="abc123",
        dry_run=True,
    )

    # Each file must use a distinct session_id to avoid context bleed between files
    assert len(session_ids_used) == 2
    assert session_ids_used[0] != session_ids_used[1], (
        "file-by-file mode must use a separate ADK session per file to avoid "
        "accumulating prior-file context in the session history"
    )


@patch("code_review.orchestration_deps.get_context_window")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_scm_config")
def test_large_pr_file_by_file_message_requests_file_diff(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
):
    """In file-by-file mode, user message explicitly asks agent to use get_pr_diff_for_file."""
    from code_review.runner import run_review

    provider = _make_provider(
        pr_files=[FileInfo(path="a.py", status="modified")],
        pr_diff="x" * 200,
    )
    _configure_common_runner_mocks(
        mock_scm,
        mock_get_provider,
        mock_llm,
        mock_context_window,
        provider=provider,
        context_window=100,
    )

    messages_sent = []

    def capture_run(*, new_message, **kwargs):
        messages_sent.append(new_message.parts[0].text if new_message.parts else "")
        return runner_run_async_returning([_make_final_event()])()

    _invoke_run_review(
        run_review,
        _make_runner(capture_run),
        "o",
        "r",
        1,
        head_sha="sha1",
        dry_run=True,
    )

    assert len(messages_sent) == 1
    msg = messages_sent[0]
    assert "get_pr_diff_for_file" in msg, (
        "message to agent should instruct use of get_pr_diff_for_file in file-by-file mode"
    )
    assert "a.py" in msg, (
        "message to agent should include the file path so the agent knows which file to review"
    )


@patch("code_review.orchestration_deps.get_context_window")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_scm_config")
def test_single_shot_mode_creates_agent_with_no_tools(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
):
    """Single-shot mode must create the agent with no tools.

    With a large context window (e.g. 1 M tokens) the full diff fits in one prompt.
    The runner embeds the diff directly in the user message, so no tool calls are
    needed.  Giving the agent tools in this mode causes it to call
    get_pr_diff_for_file / get_file_content for every file in the diff; each call
    appends to the session history and every subsequent LLM turn re-bills the entire
    accumulated context (triangular growth) — the root cause of 3 M+ token consumption
    reported on a 340 KB diff with LLM_CONTEXT_WINDOW=1000000.
    """
    from code_review.runner import run_review

    provider = _make_provider(
        pr_files=[
            FileInfo(path="a.py", status="modified"),
            FileInfo(path="b.py", status="modified"),
        ],
        pr_diff="small diff",
    )
    _configure_common_runner_mocks(
        mock_scm,
        mock_get_provider,
        mock_llm,
        mock_context_window,
        provider=provider,
        context_window=1_000_000,
    )

    agents_created = []
    messages_sent = []

    def capture_run(*, new_message, **kwargs):
        messages_sent.append(new_message.parts[0].text if new_message.parts else "")
        return runner_run_async_returning([_make_final_event()])()

    _invoke_run_review(
        run_review,
        _make_runner(capture_run),
        "o",
        "r",
        1,
        head_sha="abc123",
        dry_run=True,
        create_review_agent_side_effect=_capture_create_review_agent(agents_created),
    )

    assert len(agents_created) == 1, "single-shot mode must create exactly one agent"
    assert agents_created[0]["disable_tools"] is True, (
        "single-shot mode must pass disable_tools=True to prevent tool calls that "
        "cause triangular token accumulation"
    )
    assert len(messages_sent) == 1, "single-shot mode must make exactly one LLM call"


@patch("code_review.orchestration_deps.get_context_window")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_scm_config")
def test_file_by_file_mode_creates_agent_with_tools(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
):
    """File-by-file mode must create the agent with tools enabled.

    The agent needs get_pr_diff_for_file to fetch each file's diff.
    """
    from code_review.runner import run_review

    provider = _make_provider(
        pr_files=[FileInfo(path="a.py", status="modified")],
        pr_diff="x" * 200,
    )
    _configure_common_runner_mocks(
        mock_scm,
        mock_get_provider,
        mock_llm,
        mock_context_window,
        provider=provider,
        context_window=100,
    )

    agents_created = []

    def capture_run(*, new_message, **kwargs):
        return runner_run_async_returning([_make_final_event()])()

    _invoke_run_review(
        run_review,
        _make_runner(capture_run),
        "o",
        "r",
        1,
        head_sha="abc123",
        dry_run=True,
        create_review_agent_side_effect=_capture_create_review_agent(agents_created),
    )

    assert len(agents_created) == 1, "file-by-file mode creates exactly one agent"
    assert agents_created[0]["disable_tools"] is False, (
        "file-by-file mode must pass disable_tools=False so the agent can call "
        "get_pr_diff_for_file to fetch each file's diff"
    )


@patch("code_review.orchestration_deps.get_context_window")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_scm_config")
def test_incremental_large_review_uses_embedded_file_diffs(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
):
    """Large incremental reviews must stay scoped to the incremental diff, not the full PR diff."""
    from code_review.runner import run_review

    provider = _make_provider(
        pr_files=[FileInfo(path="legacy.py", status="modified")],
        pr_diff="FULL PR DIFF SHOULD NOT BE USED",
        incremental_pr_files=[
            FileInfo(path="a.py", status="modified"),
            FileInfo(path="b.py", status="modified"),
        ],
        incremental_pr_diff=(
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n+++ b/a.py\n"
            "@@ -1,1 +1,2 @@\n"
            " old_a\n"
            "+new_a\n"
            " context_a\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n+++ b/b.py\n"
            "@@ -5,1 +5,2 @@\n"
            " old_b\n"
            "+new_b\n"
            " context_b\n"
        ),
        capabilities=ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=False,
        ),
    )
    _configure_common_runner_mocks(
        mock_scm,
        mock_get_provider,
        mock_llm,
        mock_context_window,
        provider=provider,
        context_window=100,
        scm_overrides={"base_sha": "base123"},
    )

    agents_created = []
    messages_sent = []

    def capture_run(*, new_message, **kwargs):
        messages_sent.append(new_message.parts[0].text if new_message.parts else "")
        return runner_run_async_returning([_make_final_event()])()

    _invoke_run_review(
        run_review,
        _make_runner(capture_run),
        "o",
        "r",
        1,
        head_sha="head456",
        dry_run=True,
        create_review_agent_side_effect=_capture_create_review_agent(agents_created),
    )

    provider.get_incremental_pr_files.assert_called_once_with("o", "r", 1, "base123", "head456")
    provider.get_incremental_pr_diff.assert_called_once_with("o", "r", 1, "base123", "head456")
    provider.get_pr_files.assert_not_called()
    provider.get_pr_diff.assert_not_called()
    assert len(agents_created) == 1
    assert agents_created[0]["disable_tools"] is True, (
        "large incremental reviews must disable tools so each file prompt embeds the "
        "already-scoped diff instead of fetching the full PR file diff"
    )
    assert len(messages_sent) == 2
    assert all("Here is the unified diff for this file:" in msg for msg in messages_sent)
    assert all("get_pr_diff_for_file" not in msg for msg in messages_sent)
    assert any("<L2>+new_a" in msg for msg in messages_sent)
    assert any("<L6>+new_b" in msg for msg in messages_sent)
