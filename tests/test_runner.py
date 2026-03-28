"""Tests for runner and agent (mocked provider)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from code_review.agent import create_review_agent
from code_review.providers.base import (
    BotAttributionIdentity,
    FileInfo,
    PRInfo,
    ProviderCapabilities,
    UnresolvedReviewItem,
)
from code_review.providers.bitbucket_server import BitbucketServerProvider
from code_review.reply_dismissal_state import REPLY_DISMISSAL_ACCEPTED_REPLY_TEXT
from tests.conftest import runner_run_async_returning


class MockProvider:
    def get_pr_files(self, owner, repo, pr_number):
        return [FileInfo(path="foo.py", status="modified")]

    def get_pr_diff(self, owner, repo, pr_number):
        return "diff --git a/foo.py b/foo.py"

    def get_file_content(self, owner, repo, ref, path):
        return "content"

    def post_review_comments(self, *args, **kwargs):
        # No-op: tests assert runner behavior without touching provider implementation.
        pass

    def post_pr_summary_comment(self, owner, repo, pr_number, body):
        # No-op: summary comments are not exercised in these unit tests.
        pass

    def get_existing_review_comments(self, owner, repo, pr_number):
        return []

    def get_pr_info(self, owner, repo, pr_number):
        return None

    def get_pr_commit_messages(self, owner, repo, pr_number):
        return []

    def capabilities(self):
        return ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=False,
            supports_multiline_suggestions=False,
        )


def _scm_config(**overrides):
    m = MagicMock()
    for k, v in {
        "provider": "gitea",
        "url": "https://x.com",
        "token": "x",
        "skip_label": "",
        "skip_title_pattern": "",
        **overrides,
    }.items():
        setattr(m, k, v)
    return m


def _llm_config(**overrides):
    m = MagicMock()
    for k, v in {"provider": "gemini", "model": "model-x", **overrides}.items():
        setattr(m, k, v)
    return m


def _base_review_provider(
    *,
    capabilities: ProviderCapabilities | None = None,
) -> MagicMock:
    """Provider mock for tests that run the agent against a single modified file."""
    p = MagicMock()
    p.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    p.get_pr_diff.return_value = "diff"
    p.get_file_content.return_value = "content"
    p.get_existing_review_comments.return_value = []
    p.post_review_comments = MagicMock()
    p.post_pr_summary_comment = MagicMock()
    p.capabilities.return_value = capabilities or ProviderCapabilities(
        resolvable_comments=False,
        supports_suggestions=False,
    )
    return p


def _final_adk_event(findings_json: str) -> MagicMock:
    ev = MagicMock()
    ev.is_final_response.return_value = True
    ev.content = MagicMock()
    ev.content.parts = [MagicMock(text=findings_json)]
    return ev


def _adk_runner_single_event(findings_json: str) -> MagicMock:
    mock_event = _final_adk_event(findings_json)
    inst = MagicMock()
    inst.run_async = runner_run_async_returning([mock_event])
    return inst


def _adk_runner_n_per_file_calls(findings_json: str, n: int) -> MagicMock:
    mock_event = _final_adk_event(findings_json)
    inst = MagicMock()
    wrapper = runner_run_async_returning([mock_event])
    inst.run_async.side_effect = [wrapper() for _ in range(n)]
    return inst


def _mock_http_json_response(payload):
    mock_r = MagicMock()
    mock_r.headers = {"content-type": "application/json"}
    mock_r.raise_for_status = MagicMock()
    mock_r.json.return_value = payload
    return mock_r


def _bbs_test_comment(
    comment_id,
    text,
    *,
    state="OPEN",
    line=2,
    properties=None,
):
    comment = {
        "id": comment_id,
        "text": text,
        "state": state,
        "anchor": {"path": "f.java", "line": line},
    }
    if properties is not None:
        comment["properties"] = properties
    return comment


def _bbs_test_activity(comment):
    return {"action": "COMMENTED", "comment": comment}


def _bbs_test_page(*values):
    return {"isLastPage": True, "values": list(values)}


def _bitbucket_http_get_side_effect(*, activities, comments=None, tasks=None):
    def _get_side_effect(url: str, params=None, **kwargs):
        u = str(url)
        if "/activities" in u:
            return _mock_http_json_response(_bbs_test_page(*activities))
        if u.endswith("/comments"):
            payload = {} if comments is None else _bbs_test_page(*comments)
            return _mock_http_json_response(payload)
        if "/tasks" in u:
            return _mock_http_json_response(_bbs_test_page(*(tasks or [])))
        return _mock_http_json_response({})

    return _get_side_effect


def _reply_dismissal_app_cfg():
    return MagicMock(
        review_decision_only_skip_if_bot_not_blocking=False,
        reply_dismissal_enabled=True,
    )


def _reply_dismissal_caps(**overrides):
    defaults = {
        "resolvable_comments": False,
        "supports_suggestions": False,
        "supports_review_decisions": True,
        "supports_bot_blocking_state_query": True,
        "supports_bot_attribution_identity_query": True,
        "supports_review_thread_dismissal_context": True,
        "supports_review_thread_reply": True,
        "supports_review_thread_resolution": True,
    }
    defaults.update(overrides)
    return ProviderCapabilities(**defaults)


def _reply_dismissal_unresolved_item(
    *,
    stable_id="github:thread:PRRT_1",
    thread_id="PRRT_1",
    kind="discussion_thread",
    path="a.py",
    line=1,
    body="[High] fix it",
    inferred_severity="high",
):
    return UnresolvedReviewItem(
        stable_id=stable_id,
        thread_id=thread_id,
        kind=kind,
        path=path,
        line=line,
        body=body,
        inferred_severity=inferred_severity,
    )


def _reply_dismissal_context(
    *,
    gate_exclusion_stable_id="github:thread:PRRT_1",
    thread_id="PRRT_1",
    path="",
    line=0,
    scm_already_addressed=False,
    scm_already_addressed_reason="",
    entries=None,
):
    from code_review.schemas.review_thread_dismissal import (
        ReviewThreadDismissalContext,
        ReviewThreadDismissalEntry,
    )

    default_entries = [
        ReviewThreadDismissalEntry(comment_id="10", author_login="viper-bot", body="[High] fix it"),
        ReviewThreadDismissalEntry(comment_id="11", author_login="dev", body="done"),
    ]
    return ReviewThreadDismissalContext(
        gate_exclusion_stable_id=gate_exclusion_stable_id,
        thread_id=thread_id,
        path=path,
        line=line,
        scm_already_addressed=scm_already_addressed,
        scm_already_addressed_reason=scm_already_addressed_reason,
        entries=entries or default_entries,
    )


def _configure_reply_dismissal_provider(
    *,
    capabilities,
    unresolved_items,
    dismissal_context,
    bot_login="viper-bot",
):
    provider = _provider_with_review_decisions(capabilities=capabilities)
    provider.get_pr_info = MagicMock(return_value=PRInfo(head_sha="sha"))
    provider.get_unresolved_review_items_for_quality_gate = MagicMock(return_value=unresolved_items)
    provider.get_review_thread_dismissal_context = MagicMock(return_value=dismissal_context)
    provider.get_bot_attribution_identity = MagicMock(
        return_value=BotAttributionIdentity(login=bot_login)
    )
    return provider


def _review_decision_event_context(
    *,
    comment_id="11",
    source="webhook_comment",
    actor_login="",
    actor_id="",
):
    from code_review.schemas.review_decision_event import ReviewDecisionEventContext

    return ReviewDecisionEventContext(
        comment_id=comment_id,
        source=source,
        actor_login=actor_login,
        actor_id=actor_id,
    )


def _run_review_decision_only_with_provider(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    *,
    provider,
    head_sha="sha",
    event_context=None,
):
    from code_review.runner import run_review

    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(),
        provider=provider,
    )

    return run_review(
        "o",
        "r",
        1,
        head_sha=head_sha,
        dry_run=False,
        review_decision_only=True,
        event_context=event_context or _review_decision_event_context(),
    )


def _make_bitbucket_empty_scope_provider() -> BitbucketServerProvider:
    provider = BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0",
        "tok",
        participant_user_slug="viper",
    )
    provider.get_incremental_pr_files = MagicMock(return_value=[])
    provider.get_incremental_pr_diff = MagicMock(return_value="")
    provider.submit_review_decision = MagicMock()
    return provider


def _run_empty_incremental_scope_bitbucket_review(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    *,
    provider,
):
    from code_review.runner import run_review

    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(
            provider="bitbucket_server",
            url="https://bb:7990/rest/api/1.0",
            base_sha="base123",
        ),
        provider=provider,
    )
    return run_review("o", "r", 1, head_sha="head456", dry_run=False)


@contextmanager
def _patch_adk_runner(mock_runner_instance: MagicMock):
    with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
        yield


def test_format_reply_dismissal_user_message_marks_original_and_triggering_comments():
    from code_review.runner import _format_reply_dismissal_user_message
    from code_review.schemas.review_thread_dismissal import (
        ReviewThreadDismissalContext,
        ReviewThreadDismissalEntry,
    )

    msg = _format_reply_dismissal_user_message(
        ReviewThreadDismissalContext(
            gate_exclusion_stable_id="comment:10",
            entries=[
                ReviewThreadDismissalEntry(
                    comment_id="10",
                    author_login="viper",
                    body="[High] sanitize this",
                ),
                ReviewThreadDismissalEntry(
                    comment_id="11",
                    author_login="se",
                    body="we validate it",
                ),
                ReviewThreadDismissalEntry(
                    comment_id="12",
                    author_login="viper",
                    body="please address XML escaping specifically",
                ),
            ],
        ),
        BotAttributionIdentity(login="viper"),
        "11",
    )

    assert "Original automated review comment id: 10" in msg
    assert "Original automated review comment severity: high" in msg
    assert "Triggering human reply comment id: 11" in msg
    assert "Role: original automated review comment, bot-authored" in msg
    assert "Role: triggering human reply" in msg
    assert "Comment id: 10" in msg
    assert "Comment id: 11" in msg


@patch("code_review.runner.get_code_review_app_config")
@patch("code_review.runner._run_reply_dismissal_llm")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_reply_dismissal_sends_anchored_diff_context(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_llm,
    mock_app_cfg,
):
    from code_review.runner import run_review
    from code_review.schemas.review_decision_event import ReviewDecisionEventContext
    from code_review.schemas.review_thread_dismissal import (
        ReviewThreadDismissalContext,
        ReviewThreadDismissalEntry,
    )

    mock_app_cfg.return_value = MagicMock(
        review_decision_only_skip_if_bot_not_blocking=False,
        reply_dismissal_enabled=True,
    )
    mock_llm.return_value = '{"verdict": "agreed", "reply_text": ""}'

    caps = ProviderCapabilities(
        resolvable_comments=False,
        supports_suggestions=False,
        supports_review_decisions=True,
        supports_bot_blocking_state_query=True,
        supports_bot_attribution_identity_query=True,
        supports_review_thread_dismissal_context=True,
        supports_lightweight_pr_diff_for_file=True,
        supports_review_thread_reply=True,
        supports_review_thread_resolution=True,
    )
    provider = _provider_with_review_decisions(capabilities=caps)
    provider.get_pr_info = MagicMock(return_value=PRInfo(head_sha="sha"))
    provider.get_pr_diff_for_file = MagicMock(
        return_value=(
            "diff --git a/src/Foo.java b/src/Foo.java\n"
            "--- a/src/Foo.java\n"
            "+++ b/src/Foo.java\n"
            "@@ -4,3 +4,3 @@\n"
            " context\n"
            '-old = "unsafe"\n'
            '+new = escapeXml(input)\n'
            " tail\n"
        )
    )
    provider.get_unresolved_review_items_for_quality_gate = MagicMock(return_value=[])
    provider.get_review_thread_dismissal_context = MagicMock(
        return_value=ReviewThreadDismissalContext(
            gate_exclusion_stable_id="comment:10",
            path="src/Foo.java",
            line=5,
            entries=[
                ReviewThreadDismissalEntry(
                    comment_id="10", author_login="viper", body="[High] escape XML input"
                ),
                ReviewThreadDismissalEntry(
                    comment_id="11", author_login="dev", body="done"
                ),
            ],
        )
    )
    provider.get_bot_attribution_identity = MagicMock(return_value=BotAttributionIdentity(login="viper"))
    provider.resolve_review_thread = MagicMock()
    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(),
        provider=provider,
    )

    run_review(
        "o",
        "r",
        1,
        head_sha="sha",
        dry_run=False,
        review_decision_only=True,
        event_context=ReviewDecisionEventContext(
            comment_id="11",
            source="webhook_comment",
        ),
    )

    prompt = mock_llm.call_args.args[0]
    assert "Relevant PR diff context:" in prompt
    assert "Anchored file: src/Foo.java" in prompt
    assert "Anchored line: 5" in prompt
    assert "<L5>+new = escapeXml(input)" in prompt
    provider.get_pr_diff_for_file.assert_called_once_with("o", "r", 1, "src/Foo.java")
    assert not provider.get_pr_diff.called


def _wire_standard_runner_mocks(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    *,
    scm=None,
    provider=None,
    context_window: int = 1_000_000,
):
    mock_get_scm_config.return_value = scm if scm is not None else _scm_config()
    mock_get_provider.return_value = provider if provider is not None else _base_review_provider()
    mock_get_context_window.return_value = context_window


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

    provider = _base_review_provider()
    existing_body = "[High] Duplicate finding."
    provider.get_existing_review_comments.return_value = [
        MagicMock(
            path="foo.py",
            body=existing_body,
            model_dump=lambda: {"path": "foo.py", "body": existing_body},
        ),
    ]
    _wire_standard_runner_mocks(
        mock_get_scm_config, mock_get_provider, mock_get_context_window, provider=provider
    )

    findings_json = """[
        {"path":"foo.py","line":1,"severity":"high","code":"x","message":"Duplicate finding."},
        {"path":"foo.py","line":2,"severity":"medium","code":"y","message":"Net new finding."}
    ]"""

    with _patch_adk_runner(_adk_runner_single_event(findings_json)):
        to_post = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    assert len(to_post) == 1
    assert to_post[0].message == "Net new finding."
    provider.post_review_comments.assert_called_once()
    call_args = provider.post_review_comments.call_args
    comments = call_args[0][3]
    assert len(comments) == 1
    body = comments[0].body
    assert "[Medium] Net new finding." in body
    assert "code-review-agent:" in body and "fingerprint=" in body
    assert call_args[1]["head_sha"] == "abc123"


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_raises_when_posting_without_head_sha(
    mock_get_scm_config, mock_get_provider, mock_get_context_window
):
    """Posting comments without head_sha (dry_run=False) raises ValueError."""
    from code_review.runner import run_review

    provider = _base_review_provider()
    _wire_standard_runner_mocks(
        mock_get_scm_config, mock_get_provider, mock_get_context_window, provider=provider
    )

    findings_json = '[{"path":"foo.py","line":1,"severity":"medium","code":"x","message":"Fix."}]'

    with _patch_adk_runner(_adk_runner_single_event(findings_json)):
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

    mock_get_scm_config.return_value = _scm_config(
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

    scm_cfg = _scm_config()
    llm_cfg = _llm_config()
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

    scm_cfg = _scm_config()
    llm_cfg = _llm_config(
        temperature=0.0,
        max_output_tokens=1024,
        disable_tool_calls=False,
    )
    mock_get_scm_config.return_value = scm_cfg
    mock_get_llm_config.return_value = llm_cfg

    provider = _base_review_provider()
    provider.get_pr_files.return_value = [
        FileInfo(path="foo.py", status="modified"),
        FileInfo(path="bar.py", status="modified"),
    ]
    provider.get_pr_diff.return_value = "x" * 10_000
    mock_get_provider.return_value = provider
    mock_get_context_window.return_value = 16

    findings_json = '[{"path":"foo.py","line":1,"severity":"medium","code":"x","message":"Fix."}]'
    mock_runner = _adk_runner_n_per_file_calls(findings_json, 2)

    with _patch_adk_runner(mock_runner):
        result = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    assert len(result) == 1
    assert mock_runner.run_async.call_count == 2


def _review_decision_scm_config(**extra):
    return _scm_config(
        review_decision_enabled=True,
        review_decision_high_threshold=1,
        review_decision_medium_threshold=3,
        **extra,
    )


def _provider_with_review_decisions(
    *,
    capabilities: ProviderCapabilities | None = None,
) -> MagicMock:
    caps = capabilities or ProviderCapabilities(
        resolvable_comments=False,
        supports_suggestions=False,
        supports_review_decisions=True,
    )
    p = _base_review_provider(capabilities=caps)
    p.submit_review_decision = MagicMock()
    p.get_unresolved_review_items_for_quality_gate = MagicMock(return_value=[])
    return p


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_submits_request_changes_when_threshold_met(
    mock_get_scm_config, mock_get_provider, mock_get_context_window
):
    from code_review.runner import run_review

    provider = _provider_with_review_decisions()
    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(),
        provider=provider,
    )

    findings_json = (
        '[{"path":"foo.py","line":1,"severity":"high","code":"x","message":"Must fix."}]'
    )

    with _patch_adk_runner(_adk_runner_single_event(findings_json)):
        run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.args[3] == "REQUEST_CHANGES"


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_continues_when_submit_review_decision_raises(
    mock_get_scm_config, mock_get_provider, mock_get_context_window
):
    """Transient SCM errors on review decision must not fail the run after posting."""
    from code_review.runner import run_review

    provider = _provider_with_review_decisions()
    provider.submit_review_decision.side_effect = RuntimeError("API unavailable")
    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(),
        provider=provider,
    )

    findings_json = (
        '[{"path":"foo.py","line":1,"severity":"high","code":"x","message":"Must fix."}]'
    )

    with _patch_adk_runner(_adk_runner_single_event(findings_json)):
        posted = run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    assert len(posted) == 1
    provider.post_review_comments.assert_called_once()
    provider.submit_review_decision.assert_called_once()


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_submits_approve_when_only_low_nit_open(
    mock_get_scm_config, mock_get_provider, mock_get_context_window
):
    from code_review.runner import run_review

    provider = _provider_with_review_decisions()
    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(),
        provider=provider,
    )

    findings_json = """[
        {"path":"foo.py","line":1,"severity":"low","code":"x","message":"Optional"},
        {"path":"foo.py","line":2,"severity":"nit","code":"y","message":"Style"}
    ]"""

    with _patch_adk_runner(_adk_runner_single_event(findings_json)):
        run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.args[3] == "APPROVE"


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_dry_run_does_not_submit_review_decision(
    mock_get_scm_config, mock_get_provider, mock_get_context_window
):
    from code_review.runner import run_review

    provider = _provider_with_review_decisions()
    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(),
        provider=provider,
    )

    findings_json = (
        '[{"path":"foo.py","line":1,"severity":"high","code":"x","message":"Must fix."}]'
    )

    with _patch_adk_runner(_adk_runner_single_event(findings_json)):
        run_review("o", "r", 1, head_sha="abc123", dry_run=True)

    provider.submit_review_decision.assert_not_called()


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_request_changes_from_pre_existing_unresolved_high_comment(
    mock_get_scm_config, mock_get_provider, mock_get_context_window
):
    """Quality gate counts unresolved items from provider even when agent posts no new findings."""
    from code_review.runner import run_review

    provider = _provider_with_review_decisions()
    provider.get_unresolved_review_items_for_quality_gate = MagicMock(
        return_value=[
            UnresolvedReviewItem(
                stable_id="thread:1",
                thread_id="t1",
                kind="discussion_thread",
                path="foo.py",
                line=1,
                body="[High] Prior review",
                inferred_severity="high",
            )
        ]
    )
    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(
            provider="github",
            url="https://api.github.com",
        ),
        provider=provider,
    )

    with _patch_adk_runner(_adk_runner_single_event("[]")):
        run_review("o", "r", 1, head_sha="abc123", dry_run=False)

    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.args[3] == "REQUEST_CHANGES"


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_empty_incremental_scope_still_recomputes_review_decision(
    mock_get_scm_config, mock_get_provider, mock_get_context_window
):
    """Empty incremental diffs must still refresh the PR-level quality gate."""
    from code_review.runner import run_review

    provider = _provider_with_review_decisions()
    provider.get_incremental_pr_files = MagicMock(return_value=[])
    provider.get_incremental_pr_diff = MagicMock(return_value="")
    provider.get_unresolved_review_items_for_quality_gate = MagicMock(return_value=[])
    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(base_sha="base123"),
        provider=provider,
    )

    posted = run_review("o", "r", 1, head_sha="head456", dry_run=False)

    assert posted == []
    provider.get_incremental_pr_files.assert_called_once_with("o", "r", 1, "base123", "head456")
    provider.get_incremental_pr_diff.assert_called_once_with("o", "r", 1, "base123", "head456")
    provider.post_review_comments.assert_not_called()
    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.args[3] == "APPROVE"


@patch("code_review.providers.bitbucket_server.httpx.Client")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_empty_incremental_scope_approves_when_bitbucket_suggestion_already_applied(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_client,
):
    """Bitbucket Server empty-scope refresh must ignore applied suggestions in gate counts."""
    from code_review.runner import run_review

    mock_client.return_value.__enter__.return_value.get.side_effect = _bitbucket_http_get_side_effect(
        activities=[
            _bbs_test_activity(
                _bbs_test_comment(
                    482,
                    "[High] already applied",
                    properties={"suggestionState": "APPLIED"},
                )
            )
        ]
    )

    provider = _make_bitbucket_empty_scope_provider()
    posted = _run_empty_incremental_scope_bitbucket_review(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        provider=provider,
    )

    assert posted == []
    provider.get_incremental_pr_files.assert_called_once_with("o", "r", 1, "base123", "head456")
    provider.get_incremental_pr_diff.assert_called_once_with("o", "r", 1, "base123", "head456")
    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.args[3] == "APPROVE"


@patch("code_review.providers.bitbucket_server.httpx.Client")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_empty_incremental_scope_uses_comments_endpoint_state_for_bitbucket_gate(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_client,
):
    """Empty-scope Bitbucket refresh should honor richer /comments state over stale activities."""
    from code_review.runner import run_review

    mock_client.return_value.__enter__.return_value.get.side_effect = _bitbucket_http_get_side_effect(
        activities=[_bbs_test_activity(_bbs_test_comment(482, "[High] already applied"))],
        comments=[
            _bbs_test_comment(
                482,
                "[High] already applied",
                properties={"suggestionState": "APPLIED"},
            )
        ],
    )

    provider = _make_bitbucket_empty_scope_provider()
    posted = _run_empty_incremental_scope_bitbucket_review(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        provider=provider,
    )

    assert posted == []
    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.args[3] == "APPROVE"


@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_skips_agent_and_inline(
    mock_get_scm_config, mock_get_provider, mock_get_context_window
):
    """Decision-only mode resolves head_sha from get_pr_info and submits review without LLM."""
    from code_review.runner import run_review

    provider = _provider_with_review_decisions()
    provider.get_pr_info = MagicMock(return_value=PRInfo(head_sha="from-api-sha"))
    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(),
        provider=provider,
    )

    posted = run_review(
        "o",
        "r",
        1,
        head_sha="",
        dry_run=False,
        review_decision_only=True,
    )

    assert posted == []
    provider.post_review_comments.assert_not_called()
    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.kwargs.get("head_sha") == "from-api-sha"


@patch("code_review.runner.get_code_review_app_config")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_skips_when_skip_if_bot_not_blocking_and_reply(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_app_cfg,
):
    """Opt-in + reply_added + NOT_BLOCKING skips gate and submission."""
    from code_review.runner import run_review
    from code_review.schemas.review_decision_event import ReviewDecisionEventContext

    mock_app_cfg.return_value = MagicMock(review_decision_only_skip_if_bot_not_blocking=True)
    caps = ProviderCapabilities(
        resolvable_comments=False,
        supports_suggestions=False,
        supports_review_decisions=True,
        supports_bot_blocking_state_query=True,
    )
    provider = _provider_with_review_decisions(capabilities=caps)
    provider.get_pr_info = MagicMock(return_value=PRInfo(head_sha="from-api-sha"))
    provider.get_bot_blocking_state = MagicMock(return_value="NOT_BLOCKING")
    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(),
        provider=provider,
    )

    posted = run_review(
        "o",
        "r",
        1,
        head_sha="",
        dry_run=False,
        review_decision_only=True,
        event_context=ReviewDecisionEventContext(
            comment_id="42",
            source="webhook_comment",
        ),
    )

    assert posted == []
    provider.get_bot_blocking_state.assert_called_once_with("o", "r", 1)
    provider.submit_review_decision.assert_not_called()


@patch("code_review.runner.get_code_review_app_config")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_skip_opt_in_ignored_when_bot_blocking(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_app_cfg,
):
    from code_review.runner import run_review
    from code_review.schemas.review_decision_event import ReviewDecisionEventContext

    mock_app_cfg.return_value = MagicMock(review_decision_only_skip_if_bot_not_blocking=True)
    caps = ProviderCapabilities(
        resolvable_comments=False,
        supports_suggestions=False,
        supports_review_decisions=True,
        supports_bot_blocking_state_query=True,
    )
    provider = _provider_with_review_decisions(capabilities=caps)
    provider.get_pr_info = MagicMock(return_value=PRInfo(head_sha="sha"))
    provider.get_bot_blocking_state = MagicMock(return_value="BLOCKING")
    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(),
        provider=provider,
    )

    run_review(
        "o",
        "r",
        1,
        head_sha="",
        dry_run=False,
        review_decision_only=True,
        event_context=ReviewDecisionEventContext(
            comment_id="42",
            source="webhook_comment",
        ),
    )

    provider.submit_review_decision.assert_called_once()


@patch("code_review.runner.get_code_review_app_config")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_skip_opt_in_ignored_for_comment_deleted(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_app_cfg,
):
    from code_review.runner import run_review
    from code_review.schemas.review_decision_event import ReviewDecisionEventContext

    mock_app_cfg.return_value = MagicMock(review_decision_only_skip_if_bot_not_blocking=True)
    caps = ProviderCapabilities(
        resolvable_comments=False,
        supports_suggestions=False,
        supports_review_decisions=True,
        supports_bot_blocking_state_query=True,
    )
    provider = _provider_with_review_decisions(capabilities=caps)
    provider.get_pr_info = MagicMock(return_value=PRInfo(head_sha="sha"))
    provider.get_bot_blocking_state = MagicMock(return_value="NOT_BLOCKING")
    _wire_standard_runner_mocks(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        scm=_review_decision_scm_config(),
        provider=provider,
    )

    run_review(
        "o",
        "r",
        1,
        head_sha="",
        dry_run=False,
        review_decision_only=True,
        event_context=ReviewDecisionEventContext(
            source="webhook_comment",
        ),
    )

    provider.get_bot_blocking_state.assert_not_called()
    provider.submit_review_decision.assert_called_once()


def test_compute_quality_gate_review_outcome_matches_thresholds():
    """Shared outcome helper stays aligned with threshold semantics."""
    from code_review.runner import _compute_quality_gate_review_outcome

    provider = MagicMock()
    provider.get_unresolved_review_items_for_quality_gate = MagicMock(return_value=[])
    cfg = _review_decision_scm_config()
    from code_review.schemas.findings import FindingV1

    to_post = [
        (
            FindingV1(path="a.py", line=1, severity="high", code="c", message="m"),
            "fp1",
        )
    ]
    out = _compute_quality_gate_review_outcome(provider, "o", "r", 1, to_post, cfg)
    assert out.high_count == 1
    assert out.medium_count == 0
    assert out.decision == "REQUEST_CHANGES"
    assert "REQUEST_CHANGES" in out.submission_reason


def test_compute_quality_gate_review_outcome_excludes_stable_ids():
    from code_review.runner import _compute_quality_gate_review_outcome

    provider = MagicMock()
    provider.get_unresolved_review_items_for_quality_gate = MagicMock(
        return_value=[
            UnresolvedReviewItem(
                stable_id="github:thread:x",
                thread_id="x",
                kind="discussion_thread",
                path="a.py",
                line=1,
                body="[High] a",
                inferred_severity="high",
            ),
            UnresolvedReviewItem(
                stable_id="github:thread:y",
                thread_id="y",
                kind="discussion_thread",
                path="b.py",
                line=2,
                body="[High] b",
                inferred_severity="high",
            ),
        ]
    )
    cfg = _review_decision_scm_config()
    out = _compute_quality_gate_review_outcome(
        provider,
        "o",
        "r",
        1,
        [],
        cfg,
        excluded_gate_stable_ids=frozenset({"github:thread:x"}),
    )
    assert out.high_count == 1
    assert out.decision == "REQUEST_CHANGES"


def test_omit_marker_pr_summary_omits_meets_expectations_when_gate_requests_changes():
    from code_review.runner import (
        QualityGateReviewOutcome,
        _omit_marker_pr_summary_visible_text,
    )

    cfg = _review_decision_scm_config()
    provider = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        supports_review_decisions=True,
    )
    gate = QualityGateReviewOutcome(
        high_count=1,
        medium_count=0,
        decision="REQUEST_CHANGES",
        submission_reason="x",
    )
    text = _omit_marker_pr_summary_visible_text(
        findings_planned=0,
        successful_inline_posts=0,
        cfg=cfg,
        provider=provider,
        gate_outcome=gate,
    )
    assert "meet expectations" not in text.lower()
    assert "needs work" in text.lower()


def test_omit_marker_pr_summary_keeps_meets_expectations_when_gate_approves():
    from code_review.runner import (
        QualityGateReviewOutcome,
        _omit_marker_pr_summary_visible_text,
    )

    cfg = _review_decision_scm_config()
    provider = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        supports_review_decisions=True,
    )
    gate = QualityGateReviewOutcome(
        high_count=0,
        medium_count=0,
        decision="APPROVE",
        submission_reason="x",
    )
    text = _omit_marker_pr_summary_visible_text(
        findings_planned=0,
        successful_inline_posts=0,
        cfg=cfg,
        provider=provider,
        gate_outcome=gate,
    )
    assert "meet expectations" in text.lower()


def test_omit_marker_pr_summary_meets_expectations_when_review_decisions_disabled():
    from code_review.runner import (
        QualityGateReviewOutcome,
        _omit_marker_pr_summary_visible_text,
    )

    cfg = _scm_config(
        review_decision_enabled=False,
        review_decision_high_threshold=1,
        review_decision_medium_threshold=3,
    )
    provider = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        supports_review_decisions=True,
    )
    gate = QualityGateReviewOutcome(
        high_count=9,
        medium_count=9,
        decision="REQUEST_CHANGES",
        submission_reason="x",
    )
    text = _omit_marker_pr_summary_visible_text(
        findings_planned=0,
        successful_inline_posts=0,
        cfg=cfg,
        provider=provider,
        gate_outcome=gate,
    )
    assert "meet expectations" in text.lower()
    assert "needs work" not in text.lower()


@patch("code_review.runner.get_code_review_app_config")
@patch("code_review.runner._run_reply_dismissal_llm")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_reply_dismissal_skips_llm_when_scm_already_addressed(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_llm,
    mock_app_cfg,
):
    from code_review.schemas.review_thread_dismissal import ReviewThreadDismissalEntry

    mock_app_cfg.return_value = _reply_dismissal_app_cfg()

    provider = _configure_reply_dismissal_provider(
        capabilities=_reply_dismissal_caps(supports_suggestions=True, supports_review_thread_resolution=False),
        unresolved_items=[
            _reply_dismissal_unresolved_item(
                stable_id="comment:482",
                thread_id=None,
                kind="inline_comment",
                line=104,
                body="[Medium] apply this",
                inferred_severity="medium",
            )
        ],
        dismissal_context=_reply_dismissal_context(
            gate_exclusion_stable_id="comment:482",
            thread_id="",
            path="a.py",
            line=104,
            scm_already_addressed=True,
            scm_already_addressed_reason="suggestion_applied",
            entries=[
                ReviewThreadDismissalEntry(
                    comment_id="482", author_login="viper-bot", body="[Medium] apply this"
                ),
                ReviewThreadDismissalEntry(comment_id="483", author_login="dev", body="done"),
            ],
        ),
    )
    provider.post_review_thread_reply = MagicMock()
    provider.resolve_review_thread = MagicMock()
    _run_review_decision_only_with_provider(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        provider=provider,
        event_context=_review_decision_event_context(comment_id="483"),
    )

    mock_llm.assert_not_called()
    provider.post_review_thread_reply.assert_not_called()
    provider.resolve_review_thread.assert_not_called()
    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.args[3] == "APPROVE"


@patch("code_review.runner.get_code_review_app_config")
@patch("code_review.runner._run_reply_dismissal_llm")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_reply_dismissal_agreed_excludes_thread(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_llm,
    mock_app_cfg,
):
    mock_app_cfg.return_value = _reply_dismissal_app_cfg()
    mock_llm.return_value = '{"verdict": "agreed", "reply_text": ""}'

    provider = _configure_reply_dismissal_provider(
        capabilities=_reply_dismissal_caps(),
        unresolved_items=[_reply_dismissal_unresolved_item()],
        dismissal_context=_reply_dismissal_context(),
    )
    provider.resolve_review_thread = MagicMock()
    _run_review_decision_only_with_provider(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        provider=provider,
    )

    mock_llm.assert_called_once()
    provider.resolve_review_thread.assert_called_once()
    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.args[3] == "APPROVE"


@patch("code_review.runner.get_code_review_app_config")
@patch("code_review.runner._run_reply_dismissal_llm")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_reply_dismissal_agreed_posts_durable_reply_when_unresolvable(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_llm,
    mock_app_cfg,
):
    mock_app_cfg.return_value = _reply_dismissal_app_cfg()
    mock_llm.return_value = '{"verdict": "agreed", "reply_text": ""}'

    provider = _configure_reply_dismissal_provider(
        capabilities=_reply_dismissal_caps(supports_review_thread_resolution=False),
        unresolved_items=[
            _reply_dismissal_unresolved_item(
                stable_id="comment:10",
                thread_id=None,
                kind="inline_comment",
            )
        ],
        dismissal_context=_reply_dismissal_context(
            gate_exclusion_stable_id="comment:10",
            thread_id="",
        ),
    )
    provider.post_review_thread_reply = MagicMock()
    provider.resolve_review_thread = MagicMock()
    _run_review_decision_only_with_provider(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        provider=provider,
    )

    provider.post_review_thread_reply.assert_called_once_with(
        "o",
        "r",
        1,
        "11",
        REPLY_DISMISSAL_ACCEPTED_REPLY_TEXT,
    )
    provider.resolve_review_thread.assert_not_called()
    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.args[3] == "APPROVE"


@patch("code_review.runner.get_code_review_app_config")
@patch("code_review.runner._run_reply_dismissal_llm")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_reply_dismissal_agreed_posts_durable_reply_when_resolution_fails(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_llm,
    mock_app_cfg,
):
    mock_app_cfg.return_value = _reply_dismissal_app_cfg()
    mock_llm.return_value = '{"verdict": "agreed", "reply_text": ""}'

    provider = _configure_reply_dismissal_provider(
        capabilities=_reply_dismissal_caps(),
        unresolved_items=[_reply_dismissal_unresolved_item()],
        dismissal_context=_reply_dismissal_context(),
    )
    provider.resolve_review_thread = MagicMock(side_effect=RuntimeError("boom"))
    provider.post_review_thread_reply = MagicMock()
    _run_review_decision_only_with_provider(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        provider=provider,
    )

    provider.resolve_review_thread.assert_called_once()
    provider.post_review_thread_reply.assert_called_once_with(
        "o",
        "r",
        1,
        "11",
        REPLY_DISMISSAL_ACCEPTED_REPLY_TEXT,
    )
    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.args[3] == "APPROVE"


@patch("code_review.runner.get_code_review_app_config")
@patch("code_review.runner._run_reply_dismissal_llm")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_reply_dismissal_disagreed_posts_reply(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_llm,
    mock_app_cfg,
):
    from code_review.schemas.review_thread_dismissal import ReviewThreadDismissalEntry

    mock_app_cfg.return_value = _reply_dismissal_app_cfg()
    mock_llm.return_value = (
        '{"verdict": "disagreed", "reply_text": "Please add a regression test."}'
    )

    provider = _configure_reply_dismissal_provider(
        capabilities=_reply_dismissal_caps(supports_review_thread_resolution=False),
        unresolved_items=[],
        dismissal_context=_reply_dismissal_context(
            entries=[
                ReviewThreadDismissalEntry(comment_id="10", author_login="bot", body="[High] x"),
                ReviewThreadDismissalEntry(comment_id="11", author_login="dev", body="nope"),
            ]
        ),
        bot_login="",
    )
    provider.post_review_thread_reply = MagicMock()
    provider.get_bot_attribution_identity = MagicMock(return_value=BotAttributionIdentity())
    _run_review_decision_only_with_provider(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        provider=provider,
    )

    provider.post_review_thread_reply.assert_called_once_with(
        "o", "r", 1, "11", "Please add a regression test."
    )


@patch("code_review.runner.get_code_review_app_config")
@patch("code_review.runner._run_reply_dismissal_llm")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_reply_dismissal_skips_when_trigger_already_has_bot_reply(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_llm,
    mock_app_cfg,
):
    from code_review.schemas.review_thread_dismissal import ReviewThreadDismissalEntry

    mock_app_cfg.return_value = _reply_dismissal_app_cfg()

    provider = _configure_reply_dismissal_provider(
        capabilities=_reply_dismissal_caps(supports_review_thread_resolution=False),
        unresolved_items=[
            _reply_dismissal_unresolved_item(
                stable_id="comment:10",
                thread_id=None,
                kind="inline_comment",
                body="[High] x",
            )
        ],
        dismissal_context=_reply_dismissal_context(
            gate_exclusion_stable_id="comment:10",
            thread_id="",
            entries=[
                ReviewThreadDismissalEntry(comment_id="10", author_login="bot", body="[High] x"),
                ReviewThreadDismissalEntry(comment_id="11", author_login="dev", body="nope"),
                ReviewThreadDismissalEntry(
                    comment_id="12",
                    author_login="bot",
                    body="Still an issue; please address it.",
                ),
            ],
        ),
        bot_login="bot",
    )
    provider.post_review_thread_reply = MagicMock()
    _run_review_decision_only_with_provider(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        provider=provider,
    )

    mock_llm.assert_not_called()
    provider.post_review_thread_reply.assert_not_called()
    provider.submit_review_decision.assert_called_once()
    assert provider.submit_review_decision.call_args.args[3] == "REQUEST_CHANGES"


@patch("code_review.runner.observability.record_reply_dismissal_outcome")
@patch("code_review.runner.get_code_review_app_config")
@patch("code_review.runner._run_reply_dismissal_llm")
@patch("code_review.runner.get_context_window")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_scm_config")
def test_run_review_decision_only_skips_entire_run_when_actor_is_bot(
    mock_get_scm_config,
    mock_get_provider,
    mock_get_context_window,
    mock_llm,
    mock_app_cfg,
    mock_record_rd,
):
    """Bot-authored comment events must short-circuit review-decision-only runs."""
    mock_app_cfg.return_value = _reply_dismissal_app_cfg()

    provider = _configure_reply_dismissal_provider(
        capabilities=_reply_dismissal_caps(supports_review_thread_resolution=False),
        unresolved_items=[_reply_dismissal_unresolved_item()],
        dismissal_context=None,
    )
    provider.get_review_thread_dismissal_context = MagicMock()
    _run_review_decision_only_with_provider(
        mock_get_scm_config,
        mock_get_provider,
        mock_get_context_window,
        provider=provider,
        event_context=_review_decision_event_context(actor_login="viper-bot"),
    )

    mock_llm.assert_not_called()
    provider.get_unresolved_review_items_for_quality_gate.assert_not_called()
    provider.get_review_thread_dismissal_context.assert_not_called()
    provider.submit_review_decision.assert_not_called()
    mock_record_rd.assert_any_call("skipped_bot_author")


def test_reply_added_event_authored_by_bot_matches_login_and_id():
    from code_review.runner import _reply_added_event_authored_by_bot
    from code_review.schemas.review_decision_event import ReviewDecisionEventContext

    bot = BotAttributionIdentity(login="The-Bot", id_str="42")
    assert _reply_added_event_authored_by_bot(
        ReviewDecisionEventContext(actor_login="the-bot"), bot
    )
    assert _reply_added_event_authored_by_bot(
        ReviewDecisionEventContext(actor_id="42"), bot
    )
    assert not _reply_added_event_authored_by_bot(
        ReviewDecisionEventContext(actor_login="human"), bot
    )
    assert not _reply_added_event_authored_by_bot(
        ReviewDecisionEventContext(actor_login="human"),
        BotAttributionIdentity(),
    )
    assert not _reply_added_event_authored_by_bot(
        ReviewDecisionEventContext(comment_id="1"),
        bot,
    )
