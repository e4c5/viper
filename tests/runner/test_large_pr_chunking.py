"""Large PR fixture: validate batch-mode review execution."""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

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
        model="gemini-3.1",
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


def _make_final_event(text='{"findings":[]}'):
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=text)]
    return mock_event


def _make_runner(capture_run):
    runner = MagicMock()
    runner.run_async = capture_run
    return runner


def _invoke_run_review(
    run_review,
    runner_instance,
    *args,
    create_batch_agent_side_effect=None,
    **kwargs,
):
    with ExitStack() as stack:
        if create_batch_agent_side_effect is not None:
            stack.enter_context(
                patch(
                    "code_review.agent.workflows.create_sequential_batch_review_agent",
                    side_effect=create_batch_agent_side_effect,
                )
            )
        stack.enter_context(patch("google.adk.runners.Runner", return_value=runner_instance))
        stack.enter_context(
            patch(
                "google.adk.sessions.InMemorySessionService",
                return_value=_make_session_service(),
            )
        )
        run_review(*args, **kwargs)


def _make_session_service():
    session_service = MagicMock()
    session_service.create_session = AsyncMock()
    return session_service


def _capture_create_batch_agent(batch_agents_created):
    def capture_create_batch_agent(
        provider,
        standards="",
        batches=None,
        *,
        head_sha="",
        context_brief_attached=False,
    ):
        batch_agents_created.append(
            {
                "batches": batches,
                "head_sha": head_sha,
                "context_brief_attached": context_brief_attached,
            }
        )
        mock_agent = MagicMock()
        mock_agent.name = "sequential_batch_review_agent"
        return mock_agent

    return capture_create_batch_agent


@patch("code_review.orchestration.orchestrator.runner_mod.get_max_output_tokens", return_value=4096)
@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_large_pr_batch_mode_posts_findings_without_duplicates(
    mock_scm, mock_get_provider, mock_llm, mock_context_window, mock_get_max_output_tokens
):
    """Batch mode reviews multiple files in one runner invocation and posts unique comments."""
    del mock_get_max_output_tokens
    from code_review.runner import run_review

    provider = _make_provider(
        pr_files=[
            FileInfo(path="a.py", status="modified"),
            FileInfo(path="b.py", status="modified"),
        ],
        pr_diff=(
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n-old_a\n+new_a\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,2 @@\n-old_b\n+new_b\n"
        ),
        file_content="line1\nline2\n",
    )
    _configure_common_runner_mocks(
        mock_scm,
        mock_get_provider,
        mock_llm,
        mock_context_window,
        provider=provider,
        context_window=1_000_000,
    )

    run_calls = []

    def capture_run(*, new_message, **kwargs):
        run_calls.append(new_message)
        findings = (
            '{"findings":['
            '{"path":"a.py","line":1,"severity":"medium","code":"x","message":"Fix a."},'
            '{"path":"b.py","line":1,"severity":"low","code":"y","message":"Fix b."}'
            ']}'
        )
        event = _make_final_event(findings)
        event.author = "batch_review_0"
        return runner_run_async_returning([event])()

    _invoke_run_review(
        run_review,
        _make_runner(capture_run),
        "o",
        "r",
        1,
        head_sha="abc123",
        dry_run=False,
    )

    assert len(run_calls) == 1
    assert provider.post_review_comments.call_count == 2
    all_comments = [call[0][3][0] for call in provider.post_review_comments.call_args_list]
    assert {(c.path, c.line) for c in all_comments} == {("a.py", 1), ("b.py", 1)}


@patch("code_review.orchestration.orchestrator.runner_mod.get_max_output_tokens", return_value=4096)
@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_large_pr_batch_mode_uses_one_session(
    mock_scm, mock_get_provider, mock_llm, mock_context_window, mock_get_max_output_tokens
):
    """Batch mode uses one runner session for the whole prepared review workflow."""
    del mock_get_max_output_tokens
    from code_review.runner import run_review

    provider = _make_provider(
        pr_files=[
            FileInfo(path="a.py", status="modified"),
            FileInfo(path="b.py", status="modified"),
        ],
        pr_diff=(
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n-old_a\n+new_a\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,2 @@\n-old_b\n+new_b\n"
        ),
    )
    _configure_common_runner_mocks(
        mock_scm,
        mock_get_provider,
        mock_llm,
        mock_context_window,
        provider=provider,
        context_window=1_000_000,
    )

    session_ids_used = []

    def capture_run(*, session_id, new_message, **kwargs):
        del new_message
        session_ids_used.append(session_id)
        event = _make_final_event()
        event.author = "batch_review_0"
        return runner_run_async_returning([event])()

    _invoke_run_review(
        run_review,
        _make_runner(capture_run),
        "o",
        "r",
        1,
        head_sha="abc123",
        dry_run=True,
    )

    assert len(session_ids_used) == 1


@patch("code_review.orchestration.orchestrator.runner_mod.get_max_output_tokens", return_value=4096)
@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_large_pr_batch_mode_builds_multiple_batches_in_stable_order(
    mock_scm, mock_get_provider, mock_llm, mock_context_window, mock_get_max_output_tokens
):
    """Prepared batches preserve PR file order when diff budget forces multiple batches."""
    del mock_get_max_output_tokens
    from code_review.runner import run_review

    provider = _make_provider(
        pr_files=[
            FileInfo(path="a.py", status="modified"),
            FileInfo(path="b.py", status="modified"),
            FileInfo(path="c.py", status="modified"),
        ],
        pr_diff=(
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n-old_a\n+new_a\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,2 @@\n-old_b\n+new_b\n"
            "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -1,1 +1,2 @@\n-old_c\n+new_c\n"
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
        context_window=6_000,
    )

    batch_agents_created = []

    def capture_run(*, new_message, **kwargs):
        del new_message
        event_a = _make_final_event('{"findings":[]}')
        event_a.author = "batch_review_0"
        event_b = _make_final_event('{"findings":[]}')
        event_b.author = "batch_review_1"
        return runner_run_async_returning([event_a, event_b])()

    _invoke_run_review(
        run_review,
        _make_runner(capture_run),
        "o",
        "r",
        1,
        head_sha="sha1",
        dry_run=True,
        create_batch_agent_side_effect=_capture_create_batch_agent(batch_agents_created),
    )

    assert len(batch_agents_created) == 1
    batches = batch_agents_created[0]["batches"]
    assert len(batches) >= 2
    flattened_paths = [path for batch in batches for path in batch.paths]
    assert list(dict.fromkeys(flattened_paths))[:3] == ["a.py", "b.py", "c.py"]


@patch("code_review.orchestration.orchestrator.runner_mod.get_max_output_tokens", return_value=4096)
@patch("code_review.orchestration.orchestrator.runner_mod.get_context_window")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_incremental_large_review_builds_batches_from_incremental_diff(
    mock_scm, mock_get_provider, mock_llm, mock_context_window, mock_get_max_output_tokens
):
    """Incremental reviews must batch the incremental diff, not the full PR diff."""
    del mock_get_max_output_tokens
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
        context_window=20_000,
        scm_overrides={"base_sha": "base123"},
    )

    batch_agents_created = []

    def capture_run(*, new_message, **kwargs):
        del new_message
        event = _make_final_event('{"findings":[]}')
        event.author = "batch_review_0"
        return runner_run_async_returning([event])()

    _invoke_run_review(
        run_review,
        _make_runner(capture_run),
        "o",
        "r",
        1,
        head_sha="head456",
        dry_run=True,
        create_batch_agent_side_effect=_capture_create_batch_agent(batch_agents_created),
    )

    provider.get_incremental_pr_files.assert_called_once_with("o", "r", 1, "base123", "head456")
    provider.get_incremental_pr_diff.assert_called_once_with("o", "r", 1, "base123", "head456")
    provider.get_pr_files.assert_not_called()
    provider.get_pr_diff.assert_not_called()
    batches = batch_agents_created[0]["batches"]
    batch_text = "\n".join(segment.diff_text for batch in batches for segment in batch.segments)
    assert "FULL PR DIFF SHOULD NOT BE USED" not in batch_text
    assert "+new_a" in batch_text
    assert "+new_b" in batch_text
