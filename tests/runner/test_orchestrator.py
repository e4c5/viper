"""Unit tests for ReviewOrchestrator and its extracted helpers (RUN_REVIEW_REFACTOR_PLAN)."""

import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_review.runner import (
    ReviewOrchestrator,
    _generate_auto_pr_description,
    _maybe_post_started_review_comment,
)
from tests.conftest import runner_run_async_returning


def test_review_orchestrator_imports_without_circular_dependency():
    """Directly importing review_orchestrator should not trip runner's lazy back-import."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.path.insert(0, 'viper/src'); "
                "import code_review.review_orchestrator; "
                "print('ok')"
            ),
        ],
        cwd="/home/raditha/workspace/python/code-review",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "ok" in result.stdout


@contextmanager
def _orchestrator_run_env(
    findings_json: str = '{"findings":[{"path":"foo.py","line":1,"severity":"low","code":"c","message":"m"}]}',
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
        patch("code_review.orchestration_deps.get_context_window", return_value=1_000_000),
        patch("code_review.orchestration_deps.get_provider") as mock_get_provider,
        patch("code_review.orchestration_deps.get_scm_config") as mock_scm,
        patch("code_review.orchestration_deps.get_llm_config") as mock_llm,
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


@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_scm_config")
def test_load_config_and_provider_calls_deps_and_returns_tuple(
    mock_get_scm_config, mock_get_llm_config, mock_get_provider
):
    """_load_config_and_provider() calls get_scm_config, get_llm_config, get_provider.

    Returns (cfg, llm_cfg, provider).
    """
    cfg = MagicMock(provider="gitea", url="https://gitea.example.com", token="token123")
    cfg.bitbucket_server_user_slug = ""
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
        bitbucket_server_user_slug="",
    )
    assert result == (cfg, llm_cfg, provider)


@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_scm_config")
def test_load_config_and_provider_unwraps_secret_str(
    mock_get_scm_config, mock_get_llm_config, mock_get_provider
):
    """When cfg.token has get_secret_value(), it is called and value is passed to get_provider."""
    secret = MagicMock()
    secret.get_secret_value.return_value = "unwrapped-secret"
    cfg = MagicMock(provider="github", url="https://api.github.com", token=secret)
    cfg.bitbucket_server_user_slug = ""
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
        bitbucket_server_user_slug="",
    )


@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_scm_config")
def test_load_config_and_provider_uses_plain_token_when_no_get_secret_value(
    mock_get_scm_config, mock_get_llm_config, mock_get_provider
):
    """When cfg.token is a plain str (no get_secret_value), it is passed to get_provider as-is."""
    cfg = MagicMock(provider="gitea", url="https://x.com")
    cfg.token = "plain-token"  # plain str has no get_secret_value
    cfg.bitbucket_server_user_slug = ""
    llm_cfg = MagicMock()
    provider = MagicMock()
    mock_get_scm_config.return_value = cfg
    mock_get_llm_config.return_value = llm_cfg
    mock_get_provider.return_value = provider

    orchestrator = ReviewOrchestrator("o", "r", 1)
    orchestrator._load_config_and_provider()

    mock_get_provider.assert_called_once_with(
        "gitea", "https://x.com", "plain-token", bitbucket_server_user_slug=""
    )


@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_scm_config")
def test_load_config_and_provider_propagates_scm_config_exception(
    mock_get_scm_config, mock_get_llm_config
):
    """Exceptions from get_scm_config() propagate out of _load_config_and_provider()."""
    mock_get_scm_config.side_effect = ValueError("invalid SCM config")

    orchestrator = ReviewOrchestrator("o", "r", 1)
    with pytest.raises(ValueError, match="invalid SCM config"):
        orchestrator._load_config_and_provider()

    mock_get_llm_config.assert_not_called()


@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_scm_config")
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


@patch("google.adk.sessions.InMemorySessionService")
@patch("google.adk.runners.Runner")
@patch("code_review.agent.workflows.create_sequential_batch_review_agent")
def test_create_agent_and_runner_uses_sequential_batch_workflow(
    mock_create_sequential, mock_runner_cls, mock_session_service_cls
):
    provider = MagicMock()
    sequential_agent = MagicMock()
    mock_create_sequential.return_value = sequential_agent
    runner_instance = MagicMock()
    mock_runner_cls.return_value = runner_instance
    mock_session_service_cls.return_value = MagicMock()
    orchestrator = ReviewOrchestrator("o", "r", 1, head_sha="sha1")
    batches = [MagicMock()]

    _, _, runner = orchestrator._create_agent_and_runner(
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
    )
    assert getattr(runner, "_uses_sequential_batch_review") is True


@patch("code_review.orchestration_deps._run_agent_and_collect_responses")
def test_run_agent_and_collect_findings_parses_sequential_workflow_responses(
    mock_collect_responses,
):
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
    orchestrator = ReviewOrchestrator("o", "r", 1, head_sha="sha1")
    runner = SimpleNamespace(_uses_sequential_batch_review=True)

    findings = orchestrator._run_agent_and_collect_findings(
        runner,
        MagicMock(),
        "session-1",
        [MagicMock(), MagicMock()],
    )

    assert [(f.path, f.line, f.message) for f in findings] == [
        ("a.py", 1, "m1"),
        ("b.py", 2, "m2"),
    ]


# --- Step 2: _determine_skip_reason, _load_existing_comments_and_markers,
#            _compute_idempotency_and_maybe_short_circuit ---


def test_determine_skip_reason_returns_none_when_no_skip_config():
    """When cfg has no skip_label or skip_title_pattern, _determine_skip_reason returns None."""
    cfg = MagicMock(skip_label="", skip_title_pattern="")
    provider = MagicMock()
    o = ReviewOrchestrator("o", "r", 1)
    result = o._determine_skip_reason(provider, cfg, "trace-1", 0.0, MagicMock())
    assert result is None
    provider.get_pr_info.assert_not_called()


def test_determine_skip_reason_returns_empty_list_when_pr_has_skip_label():
    """When PR has the skip label, _determine_skip_reason returns [] and emits observability."""
    cfg = MagicMock()
    cfg.skip_label = "skip-review"
    cfg.skip_title_pattern = ""
    provider = MagicMock()
    provider.get_pr_info.return_value = MagicMock(labels=["skip-review", "other"], title="Fix bug")
    o = ReviewOrchestrator("o", "r", 1)
    with (
        patch("code_review.orchestration_deps._log_run_complete"),
        patch("code_review.orchestration_deps.observability") as mock_obs,
    ):
        result = o._determine_skip_reason(provider, cfg, "trace-1", 0.0, MagicMock())
    assert result == []
    mock_obs.finish_run.assert_called_once()


def test_determine_skip_reason_returns_none_when_pr_info_is_none():
    """When get_pr_info returns None, _determine_skip_reason returns None."""
    cfg = MagicMock(skip_label="skip-review", skip_title_pattern="")
    provider = MagicMock()
    provider.get_pr_info.return_value = None
    o = ReviewOrchestrator("o", "r", 1)
    result = o._determine_skip_reason(provider, cfg, "trace-1", 0.0, MagicMock())
    assert result is None


def test_load_existing_comments_and_markers_returns_ignore_and_resolved_sets():
    """_load_existing_comments_and_markers returns existing, dicts, ignore_set, resolved sets."""
    provider = MagicMock()
    comment = MagicMock()
    comment.model_dump.return_value = {"path": "a.py", "body": "Hello"}
    comment.path = "a.py"
    comment.body = "Hello"
    comment.resolved = False
    provider.get_existing_review_comments.return_value = [comment]

    o = ReviewOrchestrator("o", "r", 1)
    existing, existing_dicts, ignore_set, resolved_comments, resolved_body_set, resolved_fp_set = (
        o._load_existing_comments_and_markers(provider)
    )

    assert len(existing) == 1
    assert existing_dicts == [{"path": "a.py", "body": "Hello"}]
    assert len(ignore_set) >= 1  # body_hash at least
    assert resolved_comments == []
    assert resolved_body_set == set()
    assert resolved_fp_set == set()
    provider.get_existing_review_comments.assert_called_once_with("o", "r", 1)


def test_compute_idempotency_and_maybe_short_circuit_returns_none_when_no_head_sha():
    """When head_sha is empty, _compute_idempotency_and_maybe_short_circuit returns None."""
    o = ReviewOrchestrator("o", "r", 1, head_sha="")
    result = o._compute_idempotency_and_maybe_short_circuit(
        MagicMock(), MagicMock(), [], "trace", 0.0, MagicMock()
    )
    assert result is None


def test_compute_idempotency_and_maybe_short_circuit_returns_none_when_key_not_seen():
    """When idempotency key not in comments, returns None."""
    o = ReviewOrchestrator("o", "r", 1, head_sha="abc")
    result = o._compute_idempotency_and_maybe_short_circuit(
        MagicMock(),
        MagicMock(),
        [{"path": "x", "body": "no marker"}],
        "trace",
        0.0,
        MagicMock(),
    )
    assert result is None


def test_compute_idempotency_and_maybe_short_circuit_returns_empty_list_when_key_seen():
    """When idempotency key is seen in comments, returns [] and emits observability."""
    from code_review.runner import _build_idempotency_key

    cfg = MagicMock(provider="gitea", url="https://x.com", token="x")
    llm_cfg = MagicMock(provider="gemini", model="m")
    run_id = _build_idempotency_key(cfg, llm_cfg, "o", "r", 1, "abc")
    existing_dicts = [{"path": "a.py", "body": f"<!-- code-review-agent:run={run_id} -->\nDone."}]
    o = ReviewOrchestrator("o", "r", 1, head_sha="abc")
    with (
        patch("code_review.orchestration_deps._log_run_complete"),
        patch("code_review.orchestration_deps.observability") as mock_obs,
    ):
        result = o._compute_idempotency_and_maybe_short_circuit(
            cfg, llm_cfg, existing_dicts, "trace", 0.0, MagicMock()
        )
    assert result == []
    mock_obs.finish_run.assert_called_once()


def test_compute_idempotency_and_maybe_short_circuit_uses_incremental_base_in_key():
    """A different incremental base_sha must not short-circuit as the same run."""
    from code_review.runner import _build_idempotency_key

    cfg = MagicMock(provider="gitea", url="https://x.com", token="x", base_sha="base-new")
    llm_cfg = MagicMock(provider="gemini", model="m")
    run_id = _build_idempotency_key(cfg, llm_cfg, "o", "r", 1, "abc", "base-old")
    existing_dicts = [{"path": "a.py", "body": f"<!-- code-review-agent:run={run_id} -->\nDone."}]
    o = ReviewOrchestrator("o", "r", 1, head_sha="abc")

    result = o._compute_idempotency_and_maybe_short_circuit(
        cfg, llm_cfg, existing_dicts, "trace", 0.0, MagicMock()
    )

    assert result is None


# --- Step 3: _fetch_pr_files_and_diffs, _build_ignore_set_and_filter_files,
#            _detect_languages_for_files ---


def test_incremental_base_sha_uses_cfg_head_sha_when_parameter_missing():
    cfg = MagicMock(base_sha="base123", head_sha="head456")

    result = ReviewOrchestrator._incremental_base_sha(cfg, "")

    assert result == "base123"


def test_fetch_pr_files_and_diffs_returns_files_paths_and_full_diff():
    """_fetch_pr_files_and_diffs returns (files, paths, full_diff) from provider."""
    from code_review.providers.base import FileInfo

    provider = MagicMock()
    provider.get_pr_files.return_value = [
        FileInfo(path="foo.py", status="modified"),
        FileInfo(path="bar.go", status="added"),
    ]
    provider.get_pr_diff.return_value = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"

    o = ReviewOrchestrator("o", "r", 1)
    files, paths, full_diff = o._fetch_pr_files_and_diffs(provider)

    assert len(files) == 2
    assert paths == ["foo.py", "bar.go"]
    assert "diff --git" in full_diff
    provider.get_pr_files.assert_called_once_with("o", "r", 1)
    provider.get_pr_diff.assert_called_once_with("o", "r", 1)


def test_build_ignore_set_and_filter_files_returns_paths_unchanged():
    """_build_ignore_set_and_filter_files currently returns paths unchanged (no filtering)."""
    o = ReviewOrchestrator("o", "r", 1)
    paths = ["a.py", "b.go", "c.rs"]
    result = o._build_ignore_set_and_filter_files(paths)
    assert result == ["a.py", "b.go", "c.rs"]


def test_detect_languages_for_files_returns_detected_and_review_standards():
    """_detect_languages_for_files returns (detected, review_standards) from detect_from_paths."""
    o = ReviewOrchestrator("o", "r", 1)
    paths = ["src/main.py", "tests/test_foo.py"]
    detected, review_standards = o._detect_languages_for_files(paths)

    assert hasattr(detected, "language")
    assert hasattr(detected, "framework")
    assert detected.language == "python"
    assert isinstance(review_standards, str)
    assert "python" in review_standards.lower() or "Python" in review_standards


# --- Step 4: _create_agent_and_runner ---


@patch("code_review.orchestration_deps.create_review_agent")
def test_create_agent_and_runner_returns_session_id_service_runner(mock_create_agent):
    """_create_agent_and_runner returns (session_id, session_service, runner).

    Batch mode always constructs a SequentialAgent workflow over prepared batches.
    """
    del mock_create_agent
    provider = MagicMock()
    review_standards = "### Python"
    batch_agent = MagicMock()
    batches = [MagicMock()]

    o = ReviewOrchestrator("o", "r", 42)
    with (
        patch("google.adk.runners.Runner") as MockRunner,
        patch("google.adk.sessions.InMemorySessionService") as MockSessionService,
        patch("code_review.agent.workflows.create_sequential_batch_review_agent") as mock_create_batch,
    ):
        mock_svc = MagicMock()
        MockSessionService.return_value = mock_svc
        mock_runner = MagicMock()
        MockRunner.return_value = mock_runner
        mock_create_batch.return_value = batch_agent

        session_id, session_service, runner = o._create_agent_and_runner(
            provider, review_standards, batches
        )

        mock_create_batch.assert_called_once_with(
            provider,
            review_standards,
            batches,
            head_sha="",
            context_brief_attached=False,
        )
    assert session_id.startswith("o/r/pr-42/")
    assert len(session_id) > len("o/r/pr-42/")
    assert session_service is mock_svc
    assert runner is mock_runner
    # Session is created in _collect_response_async via create_session (async), not here
    mock_svc.create_session_sync.assert_not_called()
    MockRunner.assert_called_once_with(
        agent=batch_agent, app_name="code_review", session_service=mock_svc
    )


# --- Step 5: _run_agent_and_collect_findings, _attach_fingerprints_and_filter_findings,
#            _post_findings_and_summary ---


def test_attach_fingerprints_and_filter_findings_returns_to_post():
    """_attach_fingerprints_and_filter_findings filters by ignore set.

    Returns list of (finding, fp).
    """
    from code_review.schemas.findings import FindingV1

    o = ReviewOrchestrator("o", "r", 1, head_sha="abc")
    finding = FindingV1(path="foo.py", line=1, severity="low", code="X", message="msg")
    all_findings = [finding]
    provider = MagicMock()
    provider.get_file_content.return_value = "line1\nline2\n"
    ignore_set = set()
    resolved_body_set = set()
    resolved_fp_set = set()

    to_post = o._attach_fingerprints_and_filter_findings(
        all_findings,
        provider,
        ignore_set,
        resolved_body_set,
        resolved_fp_set,
    )

    assert len(to_post) == 1
    assert to_post[0][0] is finding
    assert isinstance(to_post[0][1], str)
    assert len(ignore_set) >= 1


def test_post_findings_and_summary_returns_zero_when_dry_run():
    """_post_findings_and_summary returns 0 when dry_run=True (no posts)."""
    o = ReviewOrchestrator("o", "r", 1, head_sha="abc", dry_run=True)
    provider = MagicMock()
    to_post = []
    count = o._post_findings_and_summary(
        provider, "", to_post, MagicMock(), MagicMock(), []
    )
    assert count == 0
    provider.post_review_comments.assert_not_called()


# --- Step 6: _record_observability_and_build_result ---


def test_record_observability_and_build_result_returns_findings_and_emits_log():
    """_record_observability_and_build_result calls _log_run_complete and finish_run.

    Returns findings list.
    """
    from code_review.schemas.findings import FindingV1

    o = ReviewOrchestrator("o", "r", 1)
    finding = FindingV1(path="a.py", line=1, severity="low", code="X", message="m")
    to_post = [(finding, "fp1")]
    with (
        patch("code_review.orchestration_deps._log_run_complete") as mock_log,
        patch("code_review.orchestration_deps.observability") as mock_obs,
    ):
        result = o._record_observability_and_build_result(
            "trace-1", 0.0, MagicMock(), ["a.py"], [finding], 1, to_post
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
    """Post full summary when description is missing and PR body cannot be updated."""
    provider = MagicMock()
    provider.update_pr_description = MagicMock(side_effect=NotImplementedError())
    pr_info = MagicMock(title="T", description="")
    paths = ["foo.py", "bar.py"]

    _maybe_post_started_review_comment(provider, "o", "r", 1, pr_info, paths)

    provider.post_pr_summary_comment.assert_called_once()
    args, _ = provider.post_pr_summary_comment.call_args
    assert args[0:3] == ("o", "r", 1)
    body = args[3]
    assert "Viper has started a review" in body
    assert "foo.py" in body or "bar.py" in body


def test_maybe_post_started_review_comment_updates_pr_description_when_supported():
    """Update PR description and post a short comment when the provider supports it."""
    provider = MagicMock()
    pr_info = MagicMock(title="kafka", description="")
    paths = ["AGENTS.md", "README.md"]

    _maybe_post_started_review_comment(provider, "o", "r", 1, pr_info, paths)

    provider.update_pr_description.assert_called_once()
    call_args = provider.update_pr_description.call_args[0]
    assert call_args[:3] == ("o", "r", 1)
    assert "kafka" in call_args[3] and "AGENTS.md" in call_args[3]
    provider.post_pr_summary_comment.assert_called_once()
    body = provider.post_pr_summary_comment.call_args[0][3]
    assert "Viper has started a review" in body
    assert "updated the PR description" in body
    assert "AGENTS.md" not in body  # summary is in PR description, not in comment


def test_maybe_post_started_review_comment_skips_when_description_present():
    """When PR already has a non-trivial description, no started-review comment is posted."""
    provider = MagicMock()
    pr_info = MagicMock(
        title="T",
        description="This is an existing, sufficiently detailed description for the PR.",
    )
    paths = ["foo.py"]

    _maybe_post_started_review_comment(provider, "o", "r", 1, pr_info, paths)

    provider.post_pr_summary_comment.assert_not_called()


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
        patch("code_review.orchestration_deps.get_context_window", return_value=10),
        patch("code_review.orchestration_deps.get_provider") as mock_get_provider,
        patch("code_review.orchestration_deps.get_scm_config") as mock_scm,
        patch("code_review.orchestration_deps.get_llm_config") as mock_llm,
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
    """Prepared batches must preserve explicit <L{n}> annotations in embedded diff segments."""
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

    batches = ReviewOrchestrator._build_review_batches(
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
    assert "<L10>" in annotated
    assert "<L11>" in annotated
    assert "<L12>" in annotated
    removed_lines = [ln for ln in annotated.splitlines() if "-old_11" in ln]
    assert all(not ln.strip().startswith("<L") for ln in removed_lines)
