"""Unit tests for ReviewOrchestrator and its extracted helpers (RUN_REVIEW_REFACTOR_PLAN)."""

import hashlib
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_review.config import reset_config_cache
from code_review.models import PRContext
from code_review.orchestration.orchestrator import ReviewOrchestrator
from code_review.orchestration_deps import (
    _build_idempotency_key,
    _generate_auto_pr_description,
    _maybe_post_started_review_comment,
)
from tests.conftest import runner_run_async_returning

TEST_REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_SRC_ROOT = TEST_REPO_ROOT / "src"


def test_build_batch_review_content_logs_user_prompt_when_enabled(caplog):
    from code_review.orchestration.execution import build_batch_review_content

    with patch.dict(os.environ, {"CODE_REVIEW_LOG_PROMPTS": "true"}, clear=False):
        reset_config_cache()
        try:
            caplog.set_level(logging.INFO)
            build_batch_review_content(
                pr_ctx=PRContext("o", "r", 1, head_sha="abc123"),
                batch_count=2,
                prompt_suffix="extra context",
            )
        finally:
            reset_config_cache()

    assert "LLM user prompt" in caplog.text
    assert "extra context" in caplog.text


@patch("google.adk.agents.SequentialAgent")
@patch("code_review.agent.workflows.create_review_agent")
def test_create_sequential_batch_review_agent_logs_instruction_when_enabled(
    mock_create_review_agent, _mock_sequential_agent, caplog
):
    from code_review.agent.workflows import create_sequential_batch_review_agent
    from code_review.batching import ReviewBatch, ReviewSegment

    mock_create_review_agent.return_value = MagicMock(name="code_review_agent", instruction="base")
    batch = ReviewBatch(
        batch_index=0,
        estimated_tokens=10,
        paths=("foo.py",),
        segments=(
            ReviewSegment(
                path="foo.py",
                diff_text="diff --git a/foo.py b/foo.py\n@@ -1 +1 @@\n-old\n+new\n",
                estimated_tokens=10,
                split_strategy="whole_file",
                segment_index=0,
                total_segments=1,
            ),
        ),
    )

    with patch.dict(os.environ, {"CODE_REVIEW_LOG_PROMPTS": "true"}, clear=False):
        reset_config_cache()
        try:
            caplog.set_level(logging.INFO)
            create_sequential_batch_review_agent(
                provider=MagicMock(),
                review_standards="### Python",
                batches=[batch],
                head_sha="abc123",
                review_visible_lines=None,
            )
        finally:
            reset_config_cache()

    assert "LLM instruction agent=batch_review_0" in caplog.text
    assert "Review exactly one prepared batch from this PR." in caplog.text


def test_canonical_orchestrator_imports_without_circular_dependency():
    """Directly importing the canonical orchestrator module should not trip lazy imports."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(TEST_SRC_ROOT)!r}); "
                "import code_review.orchestration.orchestrator; "
                "print('ok')"
            ),
        ],
        cwd=TEST_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "ok" in result.stdout


@contextmanager
def _orchestrator_run_env(
    findings_json: str = (
        '{"findings":[{"path":"foo.py","line":1,"severity":"low","code":"c",'
        '"message":"m"}]}'
    ),
):
    """Context manager: patch config/provider and ADK Runner; yield (provider, mock_runner)."""
    from code_review.providers.base import FileInfo

    mock_runner_instance = MagicMock()
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=findings_json)]
    mock_runner_instance.run_async = runner_run_async_returning([mock_event])

    with (
        patch(
            "code_review.orchestration.orchestrator.runner_mod.get_context_window",
            return_value=1_000_000,
        ),
        patch(
            "code_review.orchestration.orchestrator.runner_mod.get_provider"
        ) as mock_get_provider,
        patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config") as mock_scm,
        patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config") as mock_llm,
        patch("google.adk.runners.Runner", return_value=mock_runner_instance),
    ):
        mock_scm.return_value = MagicMock(
            provider="gitea",
            url="https://x.com",
            token="x",
            skip_label="",
            skip_title_pattern="",
        )
        mock_llm.return_value = MagicMock()
        provider = MagicMock()
        provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
        provider.get_pr_diff.return_value = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            "-old\n"
            "+new\n"
        )
        provider.get_existing_review_comments.return_value = []
        provider.get_file_content.return_value = "line1\n"
        provider.capabilities.return_value = MagicMock(
            resolvable_comments=False, supports_suggestions=False
        )
        mock_get_provider.return_value = provider
        yield provider, mock_runner_instance


# --- ReviewOrchestrator._load_config_and_provider() ---


@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_load_config_and_provider_calls_deps_and_returns_tuple(
    mock_get_scm_config, mock_get_llm_config, mock_get_provider
):
    """_load_config_and_provider() calls get_scm_config, get_llm_config, get_provider.

    Returns (cfg, llm_cfg, provider).
    """
    cfg = MagicMock(provider="gitea", url="https://gitea.example.com", token="token123")
    cfg.bot_identity = ""
    llm_cfg = MagicMock(provider="gemini", model="gemini-2.5-flash")
    provider = MagicMock()
    mock_get_scm_config.return_value = cfg
    mock_get_llm_config.return_value = llm_cfg
    mock_get_provider.return_value = provider

    orchestrator = ReviewOrchestrator("o", "r", 1, head_sha="abc")
    result = orchestrator._load_config_and_provider()

    mock_get_scm_config.assert_called_once()
    mock_get_llm_config.assert_called_once()
    mock_get_provider.assert_called_once_with(
        "gitea",
        "https://gitea.example.com",
        "token123",
        bot_identity="",
    )
    assert result == (cfg, llm_cfg, provider)


@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_load_config_and_provider_unwraps_secret_str(
    mock_get_scm_config, mock_get_llm_config, mock_get_provider
):
    """When cfg.token has get_secret_value(), it is called and value is passed to get_provider."""
    secret = MagicMock()
    secret.get_secret_value.return_value = "unwrapped-secret"
    cfg = MagicMock(provider="github", url="https://api.github.com", token=secret)
    cfg.bot_identity = ""
    llm_cfg = MagicMock()
    provider = MagicMock()
    mock_get_scm_config.return_value = cfg
    mock_get_llm_config.return_value = llm_cfg
    mock_get_provider.return_value = provider

    orchestrator = ReviewOrchestrator("owner", "repo", 2)
    orchestrator._load_config_and_provider()

    secret.get_secret_value.assert_called_once()
    mock_get_provider.assert_called_once_with(
        "github",
        "https://api.github.com",
        "unwrapped-secret",
        bot_identity="",
    )


@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_load_config_and_provider_uses_plain_token_when_no_get_secret_value(
    mock_get_scm_config, mock_get_llm_config, mock_get_provider
):
    """When cfg.token is a plain str (no get_secret_value), it is passed to get_provider as-is."""
    cfg = MagicMock(provider="gitea", url="https://x.com")
    cfg.token = "plain-token"  # plain str has no get_secret_value
    cfg.bot_identity = ""
    llm_cfg = MagicMock()
    provider = MagicMock()
    mock_get_scm_config.return_value = cfg
    mock_get_llm_config.return_value = llm_cfg
    mock_get_provider.return_value = provider

    orchestrator = ReviewOrchestrator("o", "r", 1)
    orchestrator._load_config_and_provider()

    mock_get_provider.assert_called_once_with(
        "gitea", "https://x.com", "plain-token", bot_identity=""
    )


@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_load_config_and_provider_propagates_scm_config_exception(
    mock_get_scm_config, mock_get_llm_config
):
    """Exceptions from get_scm_config() propagate out of _load_config_and_provider()."""
    mock_get_scm_config.side_effect = ValueError("invalid SCM config")

    orchestrator = ReviewOrchestrator("o", "r", 1)
    with pytest.raises(ValueError, match="invalid SCM config"):
        orchestrator._load_config_and_provider()

    mock_get_llm_config.assert_not_called()


@patch("code_review.orchestration.orchestrator.runner_mod.get_provider")
@patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config")
@patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config")
def test_load_config_and_provider_propagates_get_provider_exception(
    mock_get_scm_config, mock_get_llm_config, mock_get_provider
):
    """Exceptions from get_provider() propagate out of _load_config_and_provider()."""
    mock_get_scm_config.return_value = MagicMock(provider="gitea", url="https://x.com", token="x")
    mock_get_llm_config.return_value = MagicMock()
    mock_get_provider.side_effect = RuntimeError("provider init failed")

    orchestrator = ReviewOrchestrator("o", "r", 1)
    with pytest.raises(RuntimeError, match="provider init failed"):
        orchestrator._load_config_and_provider()


# --- ReviewOrchestrator construction and run() delegation ---


def test_review_orchestrator_stores_init_args():
    """ReviewOrchestrator stores owner, repo, pr_number, head_sha, dry_run, print_findings."""
    o = ReviewOrchestrator(
        "my-owner", "my-repo", 42, head_sha="sha1", dry_run=True, print_findings=True
    )
    assert o.owner == "my-owner"
    assert o.repo == "my-repo"
    assert o.pr_number == 42
    assert o.head_sha == "sha1"
    assert o.dry_run is True
    assert o.print_findings is True


def test_review_orchestrator_run_returns_list_of_findings():
    """ReviewOrchestrator.run() returns list[FindingV1] (same contract as run_review)."""
    with _orchestrator_run_env() as (provider, _):
        orchestrator = ReviewOrchestrator("o", "r", 1, head_sha="abc123", dry_run=True)
        result = orchestrator.run()

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].path == "foo.py"
    assert result[0].message == "m"


def test_review_orchestrator_run_does_not_start_observability_when_preflight_fails():
    orchestrator = ReviewOrchestrator("o", "r", 1, head_sha="abc123", dry_run=True)

    with (
        patch.object(orchestrator, "_load_config_and_provider", side_effect=RuntimeError("boom")),
        patch("code_review.orchestration.orchestrator.observability.start_run") as mock_start_run,
    ):
        with pytest.raises(RuntimeError, match="boom"):
            orchestrator.run()

    mock_start_run.assert_not_called()


@patch("google.adk.sessions.InMemorySessionService")
@patch("google.adk.runners.Runner")
@patch("code_review.agent.workflows.create_sequential_batch_review_agent")
def test_create_agent_and_runner_uses_sequential_batch_workflow(
    mock_create_sequential, mock_runner_cls, mock_session_service_cls
):
    from code_review.orchestration.execution import create_agent_and_runner

    provider = MagicMock()
    sequential_agent = MagicMock()
    mock_create_sequential.return_value = sequential_agent
    runner_instance = MagicMock()
    mock_runner_cls.return_value = runner_instance
    mock_session_service_cls.return_value = MagicMock()
    batches = [MagicMock()]

    _, _, runner = create_agent_and_runner(
        PRContext("o", "r", 1, "sha1"),
        provider,
        "review standards",
        batches,
    )

    assert runner is runner_instance
    mock_create_sequential.assert_called_once_with(
        provider,
        "review standards",
        batches,
        head_sha="sha1",
        context_brief_attached=False,
        review_visible_lines=None,
        use_output_key=False,
    )


@patch("code_review.orchestration.execution._run_sequential_batch_review_mode")
def test_execution_sequential_batch_mode_forwards_supported_args_only(mock_run_batch_mode):
    """Execution helper should receive the supported batch-review arguments only."""
    from code_review.orchestration.execution import run_agent_and_collect_findings

    provider = MagicMock()
    runner = MagicMock()
    batches = [MagicMock()]
    mock_run_batch_mode.return_value = []

    result = run_agent_and_collect_findings(
        PRContext("o", "r", 42, "abc123"),
        provider,
        "review standards",
        runner,
        "session-1",
        batches,
        context_brief_attached=True,
        prompt_suffix="extra context",
    )

    assert result == []
    mock_run_batch_mode.assert_called_once_with(
        PRContext("o", "r", 42, "abc123"),
        provider,
        "review standards",
        runner,
        "session-1",
        batches=batches,
        batch_count=1,
        context_brief_attached=True,
        prompt_suffix="extra context",
        review_visible_lines=None,
    )


@patch("code_review.orchestration.execution.runner_mod._run_agent_and_collect_response")
def test_execution_run_agent_and_collect_response_uses_canonical_runner_helper(
    mock_collect_response,
):
    from code_review.orchestration.execution import run_agent_and_collect_response

    runner = MagicMock()
    session_service = MagicMock()
    content = MagicMock()
    mock_collect_response.return_value = "final response"

    result = run_agent_and_collect_response(runner, session_service, "session-1", content)

    assert result == "final response"
    mock_collect_response.assert_called_once_with(runner, "session-1", content)


@pytest.mark.asyncio
async def test_collect_response_async_bypasses_adk_templating_for_single_response_runs():
    from code_review.orchestration.runner_utils import _collect_response_async

    agent = SimpleNamespace(instruction="Keep literal braces like {path} in prompts.")
    event = MagicMock()
    event.is_final_response.return_value = True
    event.content = MagicMock()
    event.content.parts = [MagicMock(text="final response")]
    runner = SimpleNamespace(agent=agent, run_async=runner_run_async_returning([event]))

    result = await _collect_response_async(runner, "session-1", MagicMock())

    assert result == "final response"
    assert callable(agent.instruction)
    assert agent.instruction(None) == "Keep literal braces like {path} in prompts."


@patch("code_review.orchestration.execution.runner_mod._run_agent_and_collect_responses")
def test_run_agent_and_collect_findings_parses_sequential_workflow_responses(
    mock_collect_responses,
):
    from code_review.orchestration.execution import run_agent_and_collect_findings

    mock_collect_responses.return_value = [
        (
            "batch_review_0",
            '{"findings":[{"path":"a.py","line":1,"severity":"low","code":"c1","message":"m1"}]}',
        ),
        (
            "batch_review_1",
            '{"findings":[{"path":"b.py","line":2,"severity":"medium","code":"c2","message":"m2"}]}',
        ),
    ]
    runner = SimpleNamespace(_uses_sequential_batch_review=True)

    findings = run_agent_and_collect_findings(
        PRContext("o", "r", 1, "sha1"),
        MagicMock(),
        "review standards",
        runner,
        "session-1",
        [MagicMock(), MagicMock()],
        review_visible_lines=None,
    )

    assert [(f.path, f.line, f.message) for f in findings] == [
        ("a.py", 1, "m1"),
        ("b.py", 2, "m2"),
    ]


# --- Step 2: ReviewFilter, CommentManager, _compute_idempotency_and_maybe_short_circuit ---


def test_review_filter_should_skip_returns_none_when_no_config():
    """ReviewFilter.should_skip returns None when cfg has no skip_label or skip_title_pattern."""
    from code_review.orchestration.filter import ReviewFilter

    cfg = MagicMock(skip_label="", skip_title_pattern="")
    rf = ReviewFilter()
    assert rf.should_skip(MagicMock(), cfg) is None


def test_review_filter_should_skip_returns_reason_when_label_matches():
    """ReviewFilter.should_skip returns a non-empty reason string when label is present."""
    from code_review.orchestration.filter import ReviewFilter

    cfg = MagicMock(skip_label="skip-review", skip_title_pattern="")
    pr_info = MagicMock(labels=["skip-review", "other"], title="Fix bug")
    rf = ReviewFilter()
    reason = rf.should_skip(pr_info, cfg)
    assert reason is not None
    assert "skip-review" in reason


def test_review_filter_should_skip_returns_none_when_pr_info_is_none():
    """ReviewFilter.should_skip returns None when pr_info is None."""
    from code_review.orchestration.filter import ReviewFilter

    cfg = MagicMock(skip_label="skip-review", skip_title_pattern="")
    rf = ReviewFilter()
    assert rf.should_skip(None, cfg) is None


def test_review_filter_should_skip_handles_none_labels_and_title() -> None:
    """Missing labels/title should not raise and should behave like empty inputs."""
    from code_review.orchestration.filter import ReviewFilter

    cfg = MagicMock(skip_label="skip-review", skip_title_pattern="[skip-review]")
    pr_info = MagicMock(labels=None, title=None)
    rf = ReviewFilter()

    assert rf.should_skip(pr_info, cfg) is None


def test_review_filter_should_skip_ignores_non_string_labels() -> None:
    """Skip-label matching should only consider real string labels."""
    from code_review.orchestration.filter import ReviewFilter

    cfg = MagicMock(skip_label="skip-review", skip_title_pattern="")
    pr_info = MagicMock(labels=[None, 123, " skip-review "], title="Fix bug")
    rf = ReviewFilter()

    reason = rf.should_skip(pr_info, cfg)

    assert reason is not None
    assert "skip-review" in reason


def test_comment_manager_load_existing_comments_builds_ignore_set():
    """CommentManager.load_existing_comments populates ignore_set and existing_comments."""
    from code_review.comments.manager import CommentManager

    provider = MagicMock()
    comment = MagicMock()
    comment.model_dump.return_value = {"path": "a.py", "body": "Hello"}
    comment.path = "a.py"
    comment.body = "Hello"
    comment.resolved = False
    provider.get_existing_review_comments.return_value = [comment]

    mgr = CommentManager()
    mgr.load_existing_comments(provider, "o", "r", 1)

    assert len(mgr.existing_comments) == 1
    assert len(mgr.ignore_set) >= 1  # body_hash at least
    assert mgr.resolved_fingerprints == set()
    provider.get_existing_review_comments.assert_called_once_with("o", "r", 1)


def test_comment_manager_filter_duplicates_returns_to_post():
    """CommentManager.filter_duplicates returns (finding, fp) pairs and blocks duplicates."""
    from code_review.comments.manager import CommentManager
    from code_review.schemas.findings import FindingV1

    mgr = CommentManager()  # empty ignore_set
    finding = FindingV1(path="foo.py", line=1, severity="low", code="X", message="msg")

    def fp_fn(_finding):
        return "fp-abc"

    to_post = mgr.filter_duplicates([finding], fp_fn)

    assert len(to_post) == 1
    assert to_post[0][0] is finding
    assert isinstance(to_post[0][1], str)
    # Calling again with the same finding should be deduped
    to_post2 = mgr.filter_duplicates([finding], fp_fn)
    assert to_post2 == []


def test_comment_manager_filter_duplicates_keeps_distinct_fingerprints_with_same_body():
    """Distinct fingerprints should not be suppressed by body-hash dedup seeding."""
    from code_review.comments.manager import CommentManager
    from code_review.schemas.findings import FindingV1

    mgr = CommentManager()
    finding_one = FindingV1(path="foo.py", line=1, severity="low", code="X", message="msg")
    finding_two = FindingV1(path="foo.py", line=1, severity="low", code="X", message="msg")

    fps = {
        id(finding_one): "fp-1",
        id(finding_two): "fp-2",
    }

    to_post = mgr.filter_duplicates([finding_one, finding_two], lambda f: fps[id(f)])

    assert to_post == [(finding_one, "fp-1"), (finding_two, "fp-2")]


def test_comment_manager_filter_duplicates_skips_resolved_body_hash():
    """Resolved comments should suppress reposts even without a matching fingerprint."""
    from code_review.comments.manager import CommentManager
    from code_review.formatters.comment import finding_to_comment_body
    from code_review.schemas.findings import FindingV1

    mgr = CommentManager()
    finding = FindingV1(path="foo.py", line=1, severity="low", code="X", message="msg")
    body = finding_to_comment_body(finding)
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    mgr._resolved_body_set.add((finding.path, body_hash))

    to_post = mgr.filter_duplicates([finding], lambda _finding: "")

    assert to_post == []


def test_comment_manager_filter_duplicates_uses_posting_body_format_for_dedup():
    """Dedup hashing must honor non-collapsible prompt formatting used during posting."""
    from code_review.comments.manager import CommentManager
    from code_review.formatters.comment import finding_to_comment_body
    from code_review.schemas.findings import FindingV1

    mgr = CommentManager()
    finding = FindingV1(
        path="foo.py",
        line=1,
        severity="low",
        code="X",
        message="msg",
        agent_fix_prompt="Verify src://foo.py and apply the fix.",
    )
    posted_body = finding_to_comment_body(finding, use_collapsible_prompt=False)
    posted_body_hash = hashlib.sha256(posted_body.encode()).hexdigest()
    mgr.ignore_set.add((finding.path, posted_body_hash))

    to_post = mgr.filter_duplicates(
        [finding],
        lambda _finding: "",
        use_collapsible_prompt=False,
    )

    assert to_post == []


def test_compute_idempotency_and_maybe_short_circuit_returns_none_when_no_head_sha():
    """When head_sha is empty, _compute_idempotency_and_maybe_short_circuit returns None."""
    from code_review.orchestration.runner_utils import ReviewRunObservability

    o = ReviewOrchestrator("o", "r", 1, head_sha="")
    result = o._compute_idempotency_and_maybe_short_circuit(
        MagicMock(),
        MagicMock(),
        [],
        ReviewRunObservability("trace", MagicMock(), start_time=0.0),
    )
    assert result is None


def test_compute_idempotency_and_maybe_short_circuit_returns_none_when_key_not_seen():
    """When idempotency key not in comments, returns None."""
    from code_review.orchestration.runner_utils import ReviewRunObservability

    o = ReviewOrchestrator("o", "r", 1, head_sha="abc")
    result = o._compute_idempotency_and_maybe_short_circuit(
        MagicMock(),
        MagicMock(),
        [{"path": "x", "body": "no marker"}],
        ReviewRunObservability("trace", MagicMock(), start_time=0.0),
    )
    assert result is None


def test_compute_idempotency_and_maybe_short_circuit_returns_empty_list_when_key_seen():
    """When idempotency key is seen in comments, returns [] and emits observability."""
    from code_review.orchestration.runner_utils import ReviewRunObservability

    cfg = MagicMock(provider="gitea", url="https://x.com", token="x")
    llm_cfg = MagicMock(provider="gemini", model="m")
    run_id = _build_idempotency_key(cfg, llm_cfg, "o", "r", 1, "abc")
    existing_dicts = [{"path": "a.py", "body": f"<!-- code-review-agent:run={run_id} -->\nDone."}]
    o = ReviewOrchestrator("o", "r", 1, head_sha="abc")
    with (
        patch("code_review.orchestration.runner_utils._log_run_complete"),
        patch("code_review.orchestration.runner_utils.observability") as mock_obs,
    ):
        result = o._compute_idempotency_and_maybe_short_circuit(
            cfg,
            llm_cfg,
            existing_dicts,
            ReviewRunObservability("trace", MagicMock(), start_time=0.0),
        )
    assert result == []
    mock_obs.finish_run.assert_called_once()


def test_compute_idempotency_and_maybe_short_circuit_uses_incremental_base_in_key():
    """A different incremental base_sha must not short-circuit as the same run."""
    from code_review.orchestration.runner_utils import ReviewRunObservability

    cfg = MagicMock(provider="gitea", url="https://x.com", token="x", base_sha="base-new")
    llm_cfg = MagicMock(provider="gemini", model="m")
    run_id = _build_idempotency_key(cfg, llm_cfg, "o", "r", 1, "abc", "base-old")
    existing_dicts = [{"path": "a.py", "body": f"<!-- code-review-agent:run={run_id} -->\nDone."}]
    o = ReviewOrchestrator("o", "r", 1, head_sha="abc")

    result = o._compute_idempotency_and_maybe_short_circuit(
        cfg,
        llm_cfg,
        existing_dicts,
        ReviewRunObservability("trace", MagicMock(), start_time=0.0),
    )

    assert result is None


# --- Step 3: _fetch_review_files_and_diffs, _detect_languages_for_files ---


def test_incremental_base_sha_uses_cfg_head_sha_when_parameter_missing():
    cfg = MagicMock(base_sha="base123", head_sha="head456")

    result = ReviewOrchestrator._incremental_base_sha(cfg, "")

    assert result == "base123"


def test_fetch_review_files_and_diffs_returns_files_paths_and_full_diff():
    """StandardReviewHandler.fetch_review_files_and_diffs returns the active review scope."""
    from code_review.orchestration.context_enricher import ContextEnricher
    from code_review.orchestration.reply_dismissal import ReplyDismissalHandler
    from code_review.orchestration.review_decision import ReviewDecisionHandler
    from code_review.orchestration.standard_review import StandardReviewHandler
    from code_review.providers.base import FileInfo

    provider = MagicMock()
    provider.get_pr_files.return_value = [
        FileInfo(path="foo.py", status="modified"),
        FileInfo(path="bar.go", status="added"),
    ]
    provider.get_pr_diff.return_value = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"

    pr_ctx = PRContext("o", "r", 1)
    reply_handler = ReplyDismissalHandler(
        pr_ctx, dry_run=True, event_context=None, run_reply_dismissal_llm=lambda _: ""
    )
    decision_handler = ReviewDecisionHandler(
        pr_ctx,
        dry_run=True,
        event_context=None,
        reply_dismissal_handler=reply_handler,
        result_builder=MagicMock(),
        skip_if_needed=MagicMock(),
    )
    handler = StandardReviewHandler(
        pr_ctx,
        dry_run=True,
        print_findings=False,
        context_enricher=ContextEnricher(pr_ctx),
        review_decision_handler=decision_handler,
        result_builder=MagicMock(),
    )

    files, paths, full_diff, incremental_base_sha = handler.fetch_review_files_and_diffs(
        provider,
        MagicMock(base_sha="", head_sha=""),
        incremental_base_sha_fn=ReviewOrchestrator._incremental_base_sha,
    )

    assert len(files) == 2
    assert paths == ["foo.py", "bar.go"]
    assert "diff --git" in full_diff
    assert incremental_base_sha == ""
    provider.get_pr_files.assert_called_once_with("o", "r", 1)
    provider.get_pr_diff.assert_called_once_with("o", "r", 1)


def test_detect_languages_for_files_returns_detected_and_review_standards():
    """StandardReviewHandler.detect_languages_for_files returns detector output plus standards."""
    from code_review.orchestration.standard_review import StandardReviewHandler

    paths = ["src/main.py", "tests/test_foo.py"]
    detected, review_standards = StandardReviewHandler.detect_languages_for_files(paths)

    assert hasattr(detected, "language")
    assert hasattr(detected, "framework")
    assert detected.language == "python"
    assert isinstance(review_standards, str)
    assert "python" in review_standards.lower() or "Python" in review_standards


# --- Step 4: _create_agent_and_runner ---


def test_create_agent_and_runner_returns_session_id_service_runner():
    """create_agent_and_runner returns (session_id, session_service, runner).

    Batch mode always constructs a SequentialAgent workflow over prepared batches.
    """
    from code_review.orchestration.execution import create_agent_and_runner

    provider = MagicMock()
    review_standards = "### Python"
    batch_agent = MagicMock()
    batches = [MagicMock()]

    with (
        patch("google.adk.runners.Runner") as MockRunner,
        patch("google.adk.sessions.InMemorySessionService") as MockSessionService,
        patch(
            "code_review.agent.workflows.create_sequential_batch_review_agent"
        ) as mock_create_batch,
    ):
        mock_svc = MagicMock()
        MockSessionService.return_value = mock_svc
        mock_runner = MagicMock()
        MockRunner.return_value = mock_runner
        mock_create_batch.return_value = batch_agent

        session_id, session_service, runner = create_agent_and_runner(
            PRContext("o", "r", 42),
            provider,
            review_standards,
            batches,
        )

        mock_create_batch.assert_called_once_with(
            provider,
            review_standards,
            batches,
            head_sha="",
            context_brief_attached=False,
            review_visible_lines=None,
            use_output_key=False,
        )
    assert session_id.startswith("o/r/pr-42/")
    assert len(session_id) > len("o/r/pr-42/")
    assert session_service is mock_svc
    assert runner is mock_runner
    # Runner is responsible for creating the in-memory session lazily.
    MockRunner.assert_called_once_with(
        agent=batch_agent,
        app_name="code_review",
        session_service=mock_svc,
        auto_create_session=True,
    )


def test_comment_manager_filter_duplicates_via_orchestrator_run():
    """_filter_findings_by_diff_scope integration: orchestrator.run() returns correct findings.

    This is an implicit integration test verifying CommentManager.filter_duplicates is wired
    into the full run() path.
    """
    with _orchestrator_run_env() as (provider, _):
        orchestrator = ReviewOrchestrator("o", "r", 1, head_sha="abc123", dry_run=True)
        result = orchestrator.run()

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].path == "foo.py"


def test_post_findings_and_summary_returns_zero_when_dry_run():
    """StandardReviewHandler.post_findings_and_summary returns 0 when dry_run=True."""
    from code_review.orchestration.context_enricher import ContextEnricher
    from code_review.orchestration.reply_dismissal import ReplyDismissalHandler
    from code_review.orchestration.review_decision import ReviewDecisionHandler
    from code_review.orchestration.standard_review import StandardReviewHandler

    pr_ctx = PRContext("o", "r", 1, "abc")
    reply_handler = ReplyDismissalHandler(
        pr_ctx, dry_run=True, event_context=None, run_reply_dismissal_llm=lambda _: ""
    )
    decision_handler = ReviewDecisionHandler(
        pr_ctx,
        dry_run=True,
        event_context=None,
        reply_dismissal_handler=reply_handler,
        result_builder=MagicMock(),
        skip_if_needed=MagicMock(),
    )
    handler = StandardReviewHandler(
        pr_ctx,
        dry_run=True,
        print_findings=False,
        context_enricher=ContextEnricher(pr_ctx),
        review_decision_handler=decision_handler,
        result_builder=MagicMock(),
    )
    provider = MagicMock()
    to_post = []
    count = handler.post_findings_and_summary(
        provider, "", to_post, MagicMock(), MagicMock(), []
    )
    assert count == 0
    provider.post_review_comments.assert_not_called()


# --- Step 6: _record_observability_and_build_result ---


def test_record_observability_and_build_result_returns_findings_and_emits_log():
    """_record_observability_and_build_result calls _log_run_complete and finish_run.

    Returns findings list.
    """
    from code_review.orchestration.runner_utils import ReviewRunObservability
    from code_review.schemas.findings import FindingV1

    o = ReviewOrchestrator("o", "r", 1)
    finding = FindingV1(path="a.py", line=1, severity="low", code="X", message="m")
    to_post = [(finding, "fp1")]
    with (
        patch("code_review.orchestration.runner_utils._log_run_complete") as mock_log,
        patch("code_review.orchestration.runner_utils.observability") as mock_obs,
    ):
        result = o._record_observability_and_build_result(
            ReviewRunObservability("trace-1", MagicMock(), start_time=0.0),
            ["a.py"],
            [finding],
            1,
            to_post,
        )
    assert result == [finding]
    mock_log.assert_called_once()
    mock_obs.finish_run.assert_called_once()


def test_generate_auto_pr_description_uses_title_and_paths():
    """_generate_auto_pr_description builds a simple summary from title and file paths."""
    title = "Add new feature"
    paths = ["a.py", "b.py", "a.py"]
    desc = _generate_auto_pr_description(title, paths)
    assert "Add new feature" in desc
    assert "2 file(s)" in desc
    assert "`a.py`" in desc and "`b.py`" in desc


def test_maybe_post_started_review_comment_posts_when_description_missing():
    """Post a short 'reviewing…' note when description is missing; no description update."""
    provider = MagicMock()
    pr_info = MagicMock(title="T", description="")
    paths = ["foo.py", "bar.py"]

    _maybe_post_started_review_comment(provider, PRContext("o", "r", 1), pr_info, paths)

    provider.post_pr_summary_comment.assert_called_once()
    args, _ = provider.post_pr_summary_comment.call_args
    assert args[0:3] == ("o", "r", 1)
    body = args[3]
    assert "Viper" in body
    # The static file list must NOT appear in the comment any more
    assert "foo.py" not in body and "bar.py" not in body
    # The description update must NOT be called – that is now the LLM's job
    provider.update_pr_description.assert_not_called()


def test_maybe_post_started_review_comment_does_not_update_description():
    """post_started_review_comment never calls update_pr_description (LLM does that later)."""
    provider = MagicMock()
    pr_info = MagicMock(title="kafka", description="")
    paths = ["AGENTS.md", "README.md"]

    _maybe_post_started_review_comment(provider, PRContext("o", "r", 1), pr_info, paths)

    # No description update — the LLM will write it after analysis
    provider.update_pr_description.assert_not_called()
    # But a comment IS posted to signal work has started
    provider.post_pr_summary_comment.assert_called_once()
    body = provider.post_pr_summary_comment.call_args[0][3]
    assert "Viper" in body
    assert "AGENTS.md" not in body  # file list must not appear


def test_maybe_post_started_review_comment_skips_when_description_present():
    """When PR already has a non-trivial description, no started-review comment is posted."""
    provider = MagicMock()
    pr_info = MagicMock(
        title="T",
        description="This is an existing, sufficiently detailed description for the PR.",
    )
    paths = ["foo.py"]

    _maybe_post_started_review_comment(provider, PRContext("o", "r", 1), pr_info, paths)

    provider.post_pr_summary_comment.assert_not_called()
    provider.update_pr_description.assert_not_called()


def test_maybe_post_started_review_comment_skips_when_description_is_short_but_intentional():
    """Short non-empty descriptions should not trigger the started-review comment."""
    provider = MagicMock()
    pr_info = MagicMock(
        title="T",
        description="WIP fix.",
    )
    paths = ["foo.py"]

    _maybe_post_started_review_comment(provider, PRContext("o", "r", 1), pr_info, paths)

    provider.update_pr_description.assert_not_called()
    provider.post_pr_summary_comment.assert_not_called()


def test_split_summary_for_pr_description_splits_at_walkthrough():
    """split_summary_for_pr_description returns (pre-walkthrough, walkthrough+rest)."""
    from code_review.agent.summary_agent import split_summary_for_pr_description

    full = (
        "## Summary\nNo issues found. 0 findings.\n\n"
        "## Description\nThis PR refactors the evaluator.\n\n"
        "## Walkthrough\n- Evaluator: refactored logic\n\n"
        "## Findings Overview\nNone."
    )
    desc_part, comment_part = split_summary_for_pr_description(full)

    assert "## Summary" in desc_part
    assert "## Description" in desc_part
    assert "## Walkthrough" not in desc_part
    assert "## Walkthrough" in comment_part
    assert "## Findings Overview" in comment_part


def test_split_summary_for_pr_description_no_walkthrough_returns_full_as_description():
    """When the LLM omits the Walkthrough heading, full text goes to description
    and comment is empty.
    """
    from code_review.agent.summary_agent import split_summary_for_pr_description

    full = "## Summary\nNo issues found.\n\n## Description\nSmall refactor."
    desc_part, comment_part = split_summary_for_pr_description(full)

    assert desc_part == full.strip()
    assert comment_part == ""


def test_run_does_not_post_started_review_comment_in_dry_run():
    """ReviewOrchestrator.run() must not post started-review comment when dry_run=True."""
    with _orchestrator_run_env() as (provider, _):
        provider.post_pr_summary_comment = MagicMock()
        orchestrator = ReviewOrchestrator("o", "r", 1, head_sha="abc123", dry_run=True)
        orchestrator.run()

    provider.post_pr_summary_comment.assert_not_called()


# --- Batch-mode prompt content ---


def test_run_batch_mode_message_includes_head_sha_and_batch_count():
    """Batch-mode user message should describe the sequential batch review run."""
    from unittest.mock import AsyncMock

    captured_messages: list[str] = []

    async def _capture_run_async(*args, **kwargs):
        new_msg = kwargs.get("new_message")
        if new_msg and hasattr(new_msg, "parts"):
            for part in new_msg.parts:
                if hasattr(part, "text") and part.text:
                    captured_messages.append(part.text)
        event = MagicMock()
        event.is_final_response.return_value = True
        event.content = MagicMock()
        event.content.parts = [MagicMock(text='{"findings":[]}')]
        yield event

    from code_review.providers.base import FileInfo

    with (
        patch(
            "code_review.orchestration.orchestrator.runner_mod.get_context_window",
            return_value=10,
        ),
        patch(
            "code_review.orchestration.orchestrator.runner_mod.get_provider"
        ) as mock_get_provider,
        patch("code_review.orchestration.orchestrator.runner_mod.get_scm_config") as mock_scm,
        patch("code_review.orchestration.orchestrator.runner_mod.get_llm_config") as mock_llm,
        patch("google.adk.runners.Runner") as mock_runner_cls,
        patch("google.adk.sessions.InMemorySessionService") as mock_session_svc_cls,
    ):
        mock_scm.return_value = MagicMock(
            provider="gitea",
            url="https://x.com",
            token="x",
            skip_label="",
            skip_title_pattern="",
        )
        mock_llm.return_value = MagicMock(disable_tool_calls=False)
        provider = MagicMock()
        provider.get_pr_files.return_value = [FileInfo(path="src/foo.py", status="modified")]
        provider.get_pr_diff.return_value = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@ -1,2 +1,3 @@\n"
            " line1\n"
            "+line2\n"
            " line3\n"
        )
        provider.get_existing_review_comments.return_value = []
        provider.get_file_content.return_value = "line1\nline2\n"
        provider.get_pr_info.return_value = None
        provider.capabilities.return_value = MagicMock(
            resolvable_comments=False,
            supports_suggestions=False,
            omit_fingerprint_marker_in_body=False,
            markup_supports_collapsible=False,
            markup_hides_html_comment=False,
        )
        mock_get_provider.return_value = provider

        mock_session_svc = MagicMock()
        mock_session_svc.create_session = AsyncMock()
        mock_session_svc_cls.return_value = mock_session_svc

        mock_runner_instance = MagicMock()
        mock_runner_instance.run_async = _capture_run_async
        mock_runner_cls.return_value = mock_runner_instance

        orchestrator = ReviewOrchestrator(
            "myowner", "myrepo", 42, head_sha="abc123def", dry_run=True
        )
        orchestrator.run()

    assert captured_messages
    combined = " ".join(captured_messages)
    assert "Review the prepared PR batches sequentially." in combined
    assert "abc123def" in combined
    assert "Prepared batch count:" in combined


def test_build_review_batches_preserves_annotations_for_segment_diffs():
    """Prepared batches must preserve explicit n: annotations in embedded diff segments."""
    from code_review.orchestration.execution import build_review_batches_for_scope
    from code_review.providers.base import FileInfo

    diff_with_deletion = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -10,3 +10,3 @@\n"
        " ctx_10\n"
        "-old_11\n"
        "+added_line\n"
        " ctx_12\n"
    )

    batches = build_review_batches_for_scope(
        [FileInfo(path="foo.py", status="modified")],
        ["foo.py"],
        diff_with_deletion,
        diff_budget=10_000,
    )

    assert len(batches) == 1
    segment_text = batches[0].segments[0].diff_text
    assert "@@ -10,3 +10,3 @@" in segment_text

    from code_review.diff.parser import annotate_diff_with_line_numbers

    annotated = annotate_diff_with_line_numbers(segment_text)
    assert "10: " in annotated
    assert "11:+" in annotated
    assert "12: " in annotated
    removed_lines = [ln for ln in annotated.splitlines() if "-old_11" in ln]
    assert removed_lines, "Expected '-old_11' missing from the annotated segment text"
    assert all(not ln.strip().split(':')[0].isdigit() for ln in removed_lines)


def test_build_review_batches_for_scope_falls_back_when_diff_budget_is_zero():
    from code_review.orchestration.execution import build_review_batches_for_scope
    from code_review.providers.base import FileInfo

    diff_text = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,1 +1,2 @@\n"
        "-old_line\n"
        "+new_line\n"
    )

    batches = build_review_batches_for_scope(
        [FileInfo(path="foo.py", status="modified")],
        ["foo.py"],
        diff_text,
        diff_budget=0,
    )

    assert len(batches) == 1
    assert len(batches[0].segments) == 1
    assert batches[0].segments[0].estimated_tokens > 0


def test_split_batch_for_retry_resegments_single_large_segment():
    from code_review.batching import ReviewBatch, ReviewSegment
    from code_review.diff.utils import estimate_tokens
    from code_review.orchestration.execution import _split_batch_for_retry

    first_hunk = "\n".join(
        f"+very_long_added_line_number_{i:02d}_with_extra_context_to_force_segmentation"
        for i in range(1, 25)
    )
    second_hunk = "\n".join(
        f"+second_hunk_long_added_line_number_{i:02d}_with_extra_context_to_force_segmentation"
        for i in range(25, 49)
    )
    diff_text = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,1 +1,25 @@\n"
        "-old_line\n"
        f"{first_hunk}\n"
        "@@ -40,1 +64,25 @@\n"
        "-old_line_2\n"
        f"{second_hunk}\n"
    )
    estimated_tokens = estimate_tokens(diff_text)
    batch = ReviewBatch(
        batch_index=0,
        estimated_tokens=estimated_tokens,
        segments=(
            ReviewSegment(
                path="foo.py",
                diff_text=diff_text,
                estimated_tokens=estimated_tokens,
                segment_index=0,
                total_segments=1,
                split_strategy="whole_file",
            ),
        ),
        paths=("foo.py",),
    )

    split_batches = _split_batch_for_retry(batch)

    assert len(split_batches) > 1
    assert all(len(split_batch.segments) == 1 for split_batch in split_batches)
    assert all(split_batch.paths == ("foo.py",) for split_batch in split_batches)


# --- Multiline suggested_patch enforcement at post_inline ---


def test_post_inline_strips_multiline_patch_when_platform_does_not_support_it():
    """When supports_multiline_suggestions=False, a multiline patch is cleared before posting.

    This is the defensive enforcement layer — the LLM prompt already discourages multiline
    patches on these platforms, but this ensures a rogue patch can never reach the API.
    """
    from code_review.models import PRContext
    from code_review.orchestration.posting import CommentPoster
    from code_review.providers.base import ProviderCapabilities
    from code_review.schemas.findings import FindingV1

    provider = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        supports_suggestions=True,
        supports_multiline_suggestions=False,  # e.g. Bitbucket Cloud / Server
    )

    finding = FindingV1(
        path="src/foo.py",
        line=10,
        severity="medium",
        code="bad-indent",
        message="Fix indentation.",
        suggested_patch="    if x:\n        return None",  # multiline — should be stripped
    )

    poster = CommentPoster(
        provider=provider,
        pr_ctx=PRContext("o", "r", 1, head_sha="abc123"),
    )

    captured_comments: list = []

    def _capture_post(_owner, _repo, _pr, comments, **_kw):
        captured_comments.extend(comments)

    provider.post_review_comments.side_effect = _capture_post

    poster.post_inline(
        incremental_base_sha="",
        to_post=[(finding, "fp-abc")],
        cfg=MagicMock(provider="bitbucket"),
        llm_cfg=MagicMock(),
    )

    assert len(captured_comments) == 1
    assert captured_comments[0].suggested_patch is None


def test_post_inline_preserves_single_line_patch_when_platform_does_not_support_multiline():
    """Single-line patches must NOT be stripped even when supports_multiline_suggestions=False."""
    from code_review.models import PRContext
    from code_review.orchestration.posting import CommentPoster
    from code_review.providers.base import ProviderCapabilities
    from code_review.schemas.findings import FindingV1

    provider = MagicMock()
    provider.capabilities.return_value = ProviderCapabilities(
        supports_suggestions=True,
        supports_multiline_suggestions=False,
    )

    finding = FindingV1(
        path="src/foo.py",
        line=10,
        severity="medium",
        code="rename-var",
        message="Rename variable.",
        suggested_patch="    user_id = request.user_id",  # single-line — must be kept
    )

    poster = CommentPoster(
        provider=provider,
        pr_ctx=PRContext("o", "r", 1, head_sha="abc123"),
    )

    captured_comments: list = []

    def _capture_post(_owner, _repo, _pr, comments, **_kw):
        captured_comments.extend(comments)

    provider.post_review_comments.side_effect = _capture_post

    poster.post_inline(
        incremental_base_sha="",
        to_post=[(finding, "fp-xyz")],
        cfg=MagicMock(provider="bitbucket"),
        llm_cfg=MagicMock(),
    )

    assert len(captured_comments) == 1
    assert captured_comments[0].suggested_patch == "    user_id = request.user_id"


def test_maybe_generate_and_post_summary_skips_overwrite_when_description_updated():
    from unittest.mock import MagicMock

    from code_review.models import PRContext
    from code_review.orchestration.standard_review import StandardReviewHandler

    provider = MagicMock()
    pr_ctx = PRContext("o", "r", 1)
    
    env = MagicMock()
    env.pr_info = MagicMock(description="")
    env.incremental_base_sha = ""
    env.paths = ["a.py"]
    
    handler = StandardReviewHandler(
        pr_ctx,
        dry_run=False,
        print_findings=False,
        context_enricher=MagicMock(),
        review_decision_handler=MagicMock(),
        result_builder=MagicMock(),
    )
    
    with (
        patch(
            "code_review.agent.summary_agent.split_summary_for_pr_description",
            return_value=("New Desc", "New Comment"),
        ),
        patch("code_review.agent.summary_agent.create_summary_agent"),
        patch(
            "code_review.agent.summary_agent.generate_pr_summary",
            return_value="Summary Text",
        ),
        patch("code_review.orchestration.standard_review.CommentPoster") as MockPoster,
    ):
        
        poster_instance = MagicMock()
        MockPoster.return_value = poster_instance
        
        current_pr_info = MagicMock()
        current_pr_info.description = "Someone added this in the meantime!"
        provider.get_pr_info.return_value = current_pr_info
        
        handler._maybe_generate_and_post_summary(provider, env, [(MagicMock(), True)])
        
        poster_instance.update_pr_description.assert_not_called()
        poster_instance.post_pr_summary.assert_called_once_with("New Comment")


def test_maybe_generate_and_post_summary_posts_walkthrough_when_findings_empty():
    from unittest.mock import MagicMock

    from code_review.models import PRContext
    from code_review.orchestration.standard_review import StandardReviewHandler

    provider = MagicMock()
    pr_ctx = PRContext("o", "r", 1)
    
    env = MagicMock()
    env.pr_info = MagicMock(description="")
    env.incremental_base_sha = ""
    env.paths = ["a.py"]
    
    handler = StandardReviewHandler(
        pr_ctx,
        dry_run=False,
        print_findings=False,
        context_enricher=MagicMock(),
        review_decision_handler=MagicMock(),
        result_builder=MagicMock(),
    )
    
    with (
        patch(
            "code_review.agent.summary_agent.split_summary_for_pr_description",
            return_value=("New Desc", "Walkthrough Comment"),
        ),
        patch("code_review.agent.summary_agent.create_summary_agent"),
        patch(
            "code_review.agent.summary_agent.generate_pr_summary",
            return_value="Summary Text",
        ),
        patch("code_review.orchestration.standard_review.CommentPoster") as MockPoster,
    ):
        
        poster_instance = MagicMock()
        MockPoster.return_value = poster_instance
        
        current_pr_info = MagicMock()
        current_pr_info.description = ""
        provider.get_pr_info.return_value = current_pr_info
        
        handler._maybe_generate_and_post_summary(provider, env, [])
        
        poster_instance.update_pr_description.assert_called_once_with("New Desc")
        poster_instance.post_pr_summary.assert_called_once_with("Walkthrough Comment")
