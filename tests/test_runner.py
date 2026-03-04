"""Tests for runner and agent (mocked provider)."""

from unittest.mock import MagicMock, patch

import pytest

from code_review.agent import create_review_agent
from code_review.providers.base import FileInfo, PRInfo, ProviderCapabilities


class MockProvider:
    def get_pr_files(self, owner, repo, pr_number):
        return [FileInfo(path="foo.py", status="modified")]

    def get_pr_diff(self, owner, repo, pr_number):
        return "diff --git a/foo.py b/foo.py"

    def get_file_content(self, owner, repo, ref, path):
        return "content"

    def post_review_comments(self, *args, **kwargs):
        pass

    def post_pr_summary_comment(self, owner, repo, pr_number, body):
        pass

    def get_existing_review_comments(self, owner, repo, pr_number):
        return []

    def get_pr_info(self, owner, repo, pr_number):
        return None


def test_create_review_agent():
    """Agent creation with mocked provider and review standards."""
    provider = MockProvider()
    agent = create_review_agent(provider, "### Python")
    assert agent.name == "code_review_agent"


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_ignore_list_and_posts_net_new(
    mock_get_scm_config, mock_get_provider, mock_get_context_window
):
    """Runner builds ignore set from existing comments, filters findings, posts only net-new."""
    from code_review.runner import run_review

    mock_get_scm_config.return_value = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="",
        skip_title_pattern="",
    )
    provider = MagicMock()
    provider.get_pr_files.return_value = [
        FileInfo(path="foo.py", status="modified"),
    ]
    provider.get_pr_diff.return_value = "diff"
    provider.get_file_content.return_value = "content"
    # Existing comment body matches the fully formatted body the runner will
    # generate for the duplicate finding, including severity prefix.
    existing_body = "[Critical] Duplicate finding."
    provider.get_existing_review_comments.return_value = [
        MagicMock(
            path="foo.py",
            body=existing_body,
            model_dump=lambda: {"path": "foo.py", "body": existing_body},
        ),
    ]
    provider.post_review_comments = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        resolvable_comments=False, supports_suggestions=False
    )
    provider.post_pr_summary_comment = MagicMock()
    mock_get_provider.return_value = provider
    mock_get_context_window.return_value = 1_000_000

    # Mock Runner.run to yield one final response with JSON findings (one duplicate, one net-new)
    findings_json = """[
        {"path":"foo.py","line":1,"severity":"critical","code":"x","message":"Duplicate finding."},
        {"path":"foo.py","line":2,"severity":"suggestion","code":"y","message":"Net new finding."}
    ]"""
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=findings_json)]
    mock_runner_instance = MagicMock()
    mock_runner_instance.run.return_value = iter([mock_event])

    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        to_post = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    # Duplicate (matches existing comment body hash) filtered out; only net-new
    assert len(to_post) == 1
    assert to_post[0].message == "Net new finding."
    provider.post_review_comments.assert_called_once()
    call_args = provider.post_review_comments.call_args
    comments = call_args[0][3]
    assert len(comments) == 1
    body = comments[0].body
    assert "[Suggestion] Net new finding." in body
    assert "code-review-agent:" in body and "fingerprint=" in body
    assert call_args[1]["head_sha"] == "abc123"

    # Phase 4.2: PR summary comment posted after successful inline post.
    # The runner may also post an initial "Viper has started a review" comment; assert
    # that at least one summary comment was made and that the final one contains the
    # aggregated summary text.
    assert provider.post_pr_summary_comment.call_count >= 1
    summary_body = provider.post_pr_summary_comment.call_args_list[-1][0][3]
    assert "1 Suggestion" in summary_body
    assert "See inline comments above" in summary_body


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_raises_when_posting_without_head_sha(
    mock_get_scm_config, mock_get_provider, mock_get_context_window
):
    """Posting comments without head_sha (dry_run=False) raises ValueError."""
    from code_review.runner import run_review

    mock_get_scm_config.return_value = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="",
        skip_title_pattern="",
    )
    provider = MagicMock()
    provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    provider.get_pr_diff.return_value = "diff"
    provider.get_file_content.return_value = "content"
    provider.get_existing_review_comments.return_value = []
    provider.post_review_comments = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        resolvable_comments=False, supports_suggestions=False
    )
    mock_get_provider.return_value = provider
    mock_get_context_window.return_value = 1_000_000

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

    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        with pytest.raises(ValueError, match="head_sha is required when posting"):
            run_review("o", "r", 1, head_sha="", dry_run=False)
    provider.post_review_comments.assert_not_called()


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_skips_when_pr_has_skip_label(
    mock_get_scm_config, mock_get_provider, mock_get_context_window
):
    """
    When PR has skip-review label (or title pattern), run_review returns []
    without running the agent.
    """
    from code_review.runner import run_review

    mock_get_scm_config.return_value = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="skip-review",
        skip_title_pattern="[skip-review]",
    )
    provider = MagicMock()
    provider.get_pr_info.return_value = PRInfo(
        title="WIP: do not merge",
        labels=["skip-review", "wip"],
    )
    mock_get_provider.return_value = provider
    mock_get_context_window.return_value = 1_000_000

    result = run_review("o", "r", 1, head_sha="abc")

    assert result == []
    provider.get_pr_info.assert_called_once_with("o", "r", 1)
    provider.get_pr_files.assert_not_called()
    provider.get_existing_review_comments.assert_not_called()


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_skips_when_idempotency_marker_present(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_llm_config,
    mock_get_context_window,
):
    """When an existing marker has the same run id, run_review returns [] and does not post."""
    from code_review.runner import _build_idempotency_key, run_review

    scm_cfg = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="",
        skip_title_pattern="",
    )
    llm_cfg = MagicMock(
        provider="gemini",
        model="model-x",
    )
    mock_get_scm_config.return_value = scm_cfg
    mock_get_llm_config.return_value = llm_cfg

    provider = MagicMock()
    provider.get_pr_info.return_value = None
    provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    provider.post_review_comments = MagicMock()
    provider.post_pr_summary_comment = MagicMock()
    provider.get_existing_review_comments = MagicMock()
    mock_get_provider.return_value = provider
    mock_get_context_window.return_value = 1_000_000

    head_sha = "abc123"
    run_id = _build_idempotency_key(scm_cfg, llm_cfg, "o", "r", 1, head_sha)
    marker = f"<!-- code-review-agent:fingerprint=fp123;version=0.1.0;run={run_id} -->\n\nExisting"
    existing = MagicMock(
        path="foo.py",
        body=marker,
        model_dump=lambda: {"path": "foo.py", "body": marker},
    )
    provider.get_existing_review_comments.return_value = [existing]

    result = run_review("o", "r", 1, head_sha=head_sha, dry_run=False)

    assert result == []
    provider.post_review_comments.assert_not_called()
    provider.post_pr_summary_comment.assert_not_called()


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_uses_file_by_file_mode_when_diff_exceeds_budget(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_llm_config,
    mock_get_context_window,
):
    """When diff size exceeds budget, runner reviews files one-by-one with separate sessions."""
    from code_review.runner import run_review

    scm_cfg = MagicMock(
        provider="gitea",
        url="https://x.com",
        token="x",
        skip_label="",
        skip_title_pattern="",
    )
    llm_cfg = MagicMock(
        provider="gemini",
        model="model-x",
        temperature=0.0,
        max_output_tokens=1024,
        disable_tool_calls=False,
    )
    mock_get_scm_config.return_value = scm_cfg
    mock_get_llm_config.return_value = llm_cfg

    provider = MagicMock()
    provider.get_pr_files.return_value = [
        FileInfo(path="foo.py", status="modified"),
        FileInfo(path="bar.py", status="modified"),
    ]
    # Large diff text to exceed token budget
    provider.get_pr_diff.return_value = "x" * 10_000
    provider.get_file_content.return_value = "content"
    provider.get_existing_review_comments.return_value = []
    provider.post_review_comments = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        resolvable_comments=False, supports_suggestions=False
    )
    provider.post_pr_summary_comment = MagicMock()
    mock_get_provider.return_value = provider
    # Small context window so diff_token_budget is tiny and file-by-file path is used
    mock_get_context_window.return_value = 16

    findings_json = (
        '[{"path":"foo.py","line":1,"severity":"suggestion","code":"x",'
        '"message":"Fix."}]'
    )
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=findings_json)]

    mock_runner_instance = MagicMock()
    # One run call per file (two files -> two iterators)
    mock_runner_instance.run.side_effect = [iter([mock_event]), iter([mock_event])]

    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        result = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    # We should get findings for each file (filtered down to postings)
    assert len(result) == 1
    # Runner.run called once per file
    assert mock_runner_instance.run.call_count == 2
