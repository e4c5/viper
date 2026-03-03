"""Unit tests for ReviewOrchestrator and its extracted helpers (RUN_REVIEW_REFACTOR_PLAN)."""

from unittest.mock import MagicMock, patch

import pytest

from code_review.runner import ReviewOrchestrator


# --- ReviewOrchestrator._load_config_and_provider() ---


@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_scm_config")
def test_load_config_and_provider_calls_deps_and_returns_tuple(
    mock_get_scm_config, mock_get_llm_config, mock_get_provider
):
    """_load_config_and_provider() calls get_scm_config, get_llm_config, get_provider and returns (cfg, llm_cfg, provider)."""
    cfg = MagicMock(provider="gitea", url="https://gitea.example.com", token="token123")
    llm_cfg = MagicMock(provider="gemini", model="gemini-2.5-flash")
    provider = MagicMock()
    mock_get_scm_config.return_value = cfg
    mock_get_llm_config.return_value = llm_cfg
    mock_get_provider.return_value = provider

    orchestrator = ReviewOrchestrator("o", "r", 1, head_sha="abc")
    result = orchestrator._load_config_and_provider()

    mock_get_scm_config.assert_called_once()
    mock_get_llm_config.assert_called_once()
    mock_get_provider.assert_called_once_with("gitea", "https://gitea.example.com", "token123")
    assert result == (cfg, llm_cfg, provider)


@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_scm_config")
def test_load_config_and_provider_unwraps_secret_str(
    mock_get_scm_config, mock_get_llm_config, mock_get_provider
):
    """When cfg.token has get_secret_value(), it is called and the value is passed to get_provider."""
    secret = MagicMock()
    secret.get_secret_value.return_value = "unwrapped-secret"
    cfg = MagicMock(provider="github", url="https://api.github.com", token=secret)
    llm_cfg = MagicMock()
    provider = MagicMock()
    mock_get_scm_config.return_value = cfg
    mock_get_llm_config.return_value = llm_cfg
    mock_get_provider.return_value = provider

    orchestrator = ReviewOrchestrator("owner", "repo", 2)
    orchestrator._load_config_and_provider()

    secret.get_secret_value.assert_called_once()
    mock_get_provider.assert_called_once_with("github", "https://api.github.com", "unwrapped-secret")


@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_scm_config")
def test_load_config_and_provider_uses_plain_token_when_no_get_secret_value(
    mock_get_scm_config, mock_get_llm_config, mock_get_provider
):
    """When cfg.token is a plain str (no get_secret_value), it is passed to get_provider as-is."""
    cfg = MagicMock(provider="gitea", url="https://x.com")
    cfg.token = "plain-token"  # plain str has no get_secret_value
    llm_cfg = MagicMock()
    provider = MagicMock()
    mock_get_scm_config.return_value = cfg
    mock_get_llm_config.return_value = llm_cfg
    mock_get_provider.return_value = provider

    orchestrator = ReviewOrchestrator("o", "r", 1)
    orchestrator._load_config_and_provider()

    mock_get_provider.assert_called_once_with("gitea", "https://x.com", "plain-token")


@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_scm_config")
def test_load_config_and_provider_propagates_scm_config_exception(
    mock_get_scm_config, mock_get_llm_config
):
    """Exceptions from get_scm_config() propagate out of _load_config_and_provider()."""
    mock_get_scm_config.side_effect = ValueError("invalid SCM config")

    orchestrator = ReviewOrchestrator("o", "r", 1)
    with pytest.raises(ValueError, match="invalid SCM config"):
        orchestrator._load_config_and_provider()

    mock_get_llm_config.assert_not_called()


@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_llm_config")
@patch("code_review.runner.get_scm_config")
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
    o = ReviewOrchestrator("my-owner", "my-repo", 42, head_sha="sha1", dry_run=True, print_findings=True)
    assert o.owner == "my-owner"
    assert o.repo == "my-repo"
    assert o.pr_number == 42
    assert o.head_sha == "sha1"
    assert o.dry_run is True
    assert o.print_findings is True


def test_review_orchestrator_run_returns_list_of_findings():
    """ReviewOrchestrator.run() returns list[FindingV1] (same contract as run_review)."""
    with patch("code_review.runner.get_context_window", return_value=1_000_000), patch(
        "code_review.runner.get_provider"
    ) as mock_get_provider, patch("code_review.runner.get_scm_config") as mock_scm, patch(
        "code_review.runner.get_llm_config"
    ) as mock_llm:
        from code_review.providers.base import FileInfo

        mock_scm.return_value = MagicMock(
            provider="gitea", url="https://x.com", token="x", skip_label="", skip_title_pattern=""
        )
        mock_llm.return_value = MagicMock()
        provider = MagicMock()
        provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
        provider.get_pr_diff.return_value = "diff"
        provider.get_existing_review_comments.return_value = []
        provider.get_file_content.return_value = "line1\n"
        provider.capabilities.return_value = MagicMock(resolvable_comments=False, supports_suggestions=False)
        mock_get_provider.return_value = provider

        mock_runner_instance = MagicMock()
        findings_json = '[{"path":"foo.py","line":1,"severity":"info","code":"c","message":"m"}]'
        mock_event = MagicMock()
        mock_event.is_final_response.return_value = True
        mock_event.content = MagicMock()
        mock_event.content.parts = [MagicMock(text=findings_json)]
        mock_runner_instance.run.return_value = iter([mock_event])

        with patch("google.adk.runners.Runner", return_value=mock_runner_instance):
            orchestrator = ReviewOrchestrator("o", "r", 1, head_sha="abc123", dry_run=True)
            result = orchestrator.run()

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].path == "foo.py"
    assert result[0].message == "m"


# --- Step 2: _determine_skip_reason, _load_existing_comments_and_markers, _compute_idempotency_and_maybe_short_circuit ---


def test_determine_skip_reason_returns_none_when_no_skip_config():
    """When cfg has no skip_label or skip_title_pattern, _determine_skip_reason returns None."""
    cfg = MagicMock(skip_label="", skip_title_pattern="")
    provider = MagicMock()
    o = ReviewOrchestrator("o", "r", 1)
    result = o._determine_skip_reason(
        provider, cfg, "o", "r", 1, "trace-1", 0.0, MagicMock()
    )
    assert result is None
    provider.get_pr_info.assert_not_called()


def test_determine_skip_reason_returns_empty_list_when_pr_has_skip_label():
    """When PR has the skip label, _determine_skip_reason returns [] and emits observability."""
    cfg = MagicMock()
    cfg.skip_label = "skip-review"
    cfg.skip_title_pattern = ""
    provider = MagicMock()
    provider.get_pr_info.return_value = MagicMock(
        labels=["skip-review", "other"], title="Fix bug"
    )
    o = ReviewOrchestrator("o", "r", 1)
    with patch("code_review.runner._log_run_complete"), patch(
        "code_review.runner.observability"
    ) as mock_obs:
        result = o._determine_skip_reason(
            provider, cfg, "o", "r", 1, "trace-1", 0.0, MagicMock()
        )
    assert result == []
    mock_obs.finish_run.assert_called_once()


def test_determine_skip_reason_returns_none_when_pr_info_is_none():
    """When get_pr_info returns None, _determine_skip_reason returns None."""
    cfg = MagicMock(skip_label="skip-review", skip_title_pattern="")
    provider = MagicMock()
    provider.get_pr_info.return_value = None
    o = ReviewOrchestrator("o", "r", 1)
    result = o._determine_skip_reason(
        provider, cfg, "o", "r", 1, "trace-1", 0.0, MagicMock()
    )
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
        o._load_existing_comments_and_markers(provider, "o", "r", 1)
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
        MagicMock(), MagicMock(), "o", "r", 1, "", [], "trace", 0.0, MagicMock()
    )
    assert result is None


def test_compute_idempotency_and_maybe_short_circuit_returns_none_when_key_not_seen():
    """When idempotency key not in comments, returns None."""
    o = ReviewOrchestrator("o", "r", 1, head_sha="abc")
    result = o._compute_idempotency_and_maybe_short_circuit(
        MagicMock(),
        MagicMock(),
        "o",
        "r",
        1,
        "abc",
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
    existing_dicts = [
        {"path": "a.py", "body": f"<!-- code-review-agent:run={run_id} -->\nDone."}
    ]
    o = ReviewOrchestrator("o", "r", 1, head_sha="abc")
    with patch("code_review.runner._log_run_complete"), patch(
        "code_review.runner.observability"
    ) as mock_obs:
        result = o._compute_idempotency_and_maybe_short_circuit(
            cfg, llm_cfg, "o", "r", 1, "abc", existing_dicts, "trace", 0.0, MagicMock()
        )
    assert result == []
    mock_obs.finish_run.assert_called_once()
