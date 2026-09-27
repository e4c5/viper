"""Tests for multi skip labels, min_severity, max_findings, custom instructions,
and the LLMConfig diff_budget_ratio."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from code_review.config import CodeReviewAppConfig, LLMConfig, SCMConfig
from code_review.orchestration.filter import ReviewFilter, configured_skip_labels
from code_review.schemas.findings import FindingV1


def _scm(**kw):
    return SCMConfig(
        provider="gitea", url="https://x.example", token="t", **kw
    )


def test_skip_labels_single():
    assert _scm().skip_labels() == ["skip-review"]
    assert _scm(skip_label="wip").skip_labels() == ["wip"]


def test_skip_labels_multiple_order_preserved():
    cfg = _scm(skip_label="skip-review, wip ,DO-NOT-REVIEW")
    assert cfg.skip_labels() == ["skip-review", "wip", "DO-NOT-REVIEW"]


def test_skip_labels_empty_disables():
    assert _scm(skip_label="").skip_labels() == []
    assert _scm(skip_label=" , ,").skip_labels() == []


def test_configured_skip_labels_tolerates_mocks():
    cfg = MagicMock()
    cfg.skip_label = MagicMock()  # not a string
    cfg.skip_labels = MagicMock(return_value=MagicMock())  # not a list
    assert configured_skip_labels(cfg) == []


def _pr_info(labels=None, title=""):
    return SimpleNamespace(labels=labels or [], title=title)


def test_filter_matches_any_skip_label_case_insensitive():
    cfg = _scm(skip_label="skip-review, wip")
    rf = ReviewFilter()
    reason = rf.should_skip(_pr_info(labels=["WIP"]), cfg)
    assert reason == "PR has skip label: wip"


def test_filter_reason_names_matched_label_not_config():
    cfg = _scm(skip_label="first, second")
    rf = ReviewFilter()
    assert rf.should_skip(_pr_info(labels=["SECOND"]), cfg) == "PR has skip label: second"


def test_filter_empty_skip_label_disables_label_check():
    cfg = _scm(skip_label="", skip_title_pattern="")
    rf = ReviewFilter()
    assert rf.should_skip(_pr_info(labels=["wip"], title="x"), cfg) is None


def test_filter_title_pattern_still_applies():
    cfg = _scm(skip_label="", skip_title_pattern="[skip-review]")
    rf = ReviewFilter()
    reason = rf.should_skip(_pr_info(title="[Skip-Review] big change"), cfg)
    assert reason == "PR title matches skip pattern: [skip-review]"


# ---------------------------------------------------------------------------
# LLMConfig.diff_budget_ratio
# ---------------------------------------------------------------------------


def test_diff_budget_ratio_default_and_env(monkeypatch):
    monkeypatch.delenv("LLM_DIFF_BUDGET_RATIO", raising=False)
    assert LLMConfig().diff_budget_ratio == 0.5
    monkeypatch.setenv("LLM_DIFF_BUDGET_RATIO", "0.25")
    assert LLMConfig().diff_budget_ratio == 0.25


@pytest.mark.parametrize("bad", ["0", "1.5", "-0.5"])
def test_diff_budget_ratio_out_of_range_rejected(monkeypatch, bad):
    monkeypatch.setenv("LLM_DIFF_BUDGET_RATIO", bad)
    with pytest.raises(ValidationError):
        LLMConfig()


def test_execute_review_agent_uses_llm_config_diff_budget_ratio():
    """standard_review must read diff_budget_ratio from the effective LLM config."""
    from code_review.orchestration import standard_review as sr

    captured = {}

    def fake_budget(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            effective_diff_budget_tokens=1000, prompt_budget_tokens=1000
        )

    handler = sr.StandardReviewHandler(
        sr.PRContext("o", "r", 1, "sha"),
        dry_run=True,
        print_findings=False,
        context_enricher=MagicMock(),
        review_decision_handler=MagicMock(),
        result_builder=MagicMock(),
    )
    handler.context_enricher.build_prompt_suffix.return_value = ([], None, "")

    env = SimpleNamespace(
        files=[], paths=[], full_diff="", incremental_base_sha=None, pr_info=None
    )
    llm_cfg = LLMConfig(diff_budget_ratio=0.3)

    with (
        patch.object(sr, "build_review_batch_budget", fake_budget),
        patch.object(
            sr.runner_mod, "get_context_window_for_config", return_value=128000
        ),
        patch.object(
            sr.runner_mod, "get_max_output_tokens_for_config", return_value=4096
        ),
        patch.object(sr.runner_mod, "get_context_aware_config", return_value=MagicMock()),
    ):
        handler._execute_review_agent(
            provider=MagicMock(),
            cfg=MagicMock(),
            agent_llm_config=llm_cfg,
            app_cfg=SimpleNamespace(
                include_commit_messages_in_prompt=False, custom_instructions=None
            ),
            run_observability=MagicMock(),
            env=env,
            review_standards=None,
        )

    assert captured["diff_budget_ratio"] == 0.3


def test_execute_review_agent_falls_back_to_global_llm_config(monkeypatch):
    from code_review.orchestration import standard_review as sr

    captured = {}

    def fake_budget(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            effective_diff_budget_tokens=1000, prompt_budget_tokens=1000
        )

    handler = sr.StandardReviewHandler(
        sr.PRContext("o", "r", 1, "sha"),
        dry_run=True,
        print_findings=False,
        context_enricher=MagicMock(),
        review_decision_handler=MagicMock(),
        result_builder=MagicMock(),
    )
    handler.context_enricher.build_prompt_suffix.return_value = ([], None, "")

    env = SimpleNamespace(
        files=[], paths=[], full_diff="", incremental_base_sha=None, pr_info=None
    )
    with (
        patch.object(sr, "build_review_batch_budget", fake_budget),
        patch.object(sr.runner_mod, "get_context_window", return_value=128000),
        patch.object(sr.runner_mod, "get_max_output_tokens", return_value=4096),
        patch.object(
            sr.runner_mod,
            "get_llm_config",
            return_value=LLMConfig(diff_budget_ratio=0.7),
        ),
        patch.object(sr.runner_mod, "get_context_aware_config", return_value=MagicMock()),
    ):
        handler._execute_review_agent(
            provider=MagicMock(),
            cfg=MagicMock(),
            agent_llm_config=None,
            app_cfg=SimpleNamespace(
                include_commit_messages_in_prompt=False, custom_instructions=None
            ),
            run_observability=MagicMock(),
            env=env,
            review_standards=None,
        )

    assert captured["diff_budget_ratio"] == 0.7


# ---------------------------------------------------------------------------
# min_severity / max_findings in the refinement funnel
# ---------------------------------------------------------------------------


def _finding(severity, confidence=None, code="c", path="a.py", line=1):
    return FindingV1(
        path=path, line=line, severity=severity, code=code,
        message="m", confidence=confidence,
    )


def _funnel(app_cfg, pairs):
    """Run _refine_findings_funnel with scope/dup/verify neutralised."""
    from code_review.orchestration import standard_review as sr

    handler = sr.StandardReviewHandler(
        sr.PRContext("o", "r", 1, "sha"),
        dry_run=True,
        print_findings=False,
        context_enricher=MagicMock(),
        review_decision_handler=MagicMock(),
        result_builder=MagicMock(),
    )
    handler.filter_findings_by_diff_scope = lambda f, paths, diff, **kw: f
    comment_mgr = MagicMock()
    comment_mgr.filter_duplicates = (
        lambda findings, *a, **kw: [(f, f"fp-{f.code}") for f in findings]
    )
    env = SimpleNamespace(paths=["a.py"], full_diff="", pr_info=None)

    with patch(
        "code_review.agent.verification_agent.verify_findings",
        lambda findings, diff: findings,
    ):
        to_post, _ = handler._refine_findings_funnel(
            MagicMock(), app_cfg, env, comment_mgr, list(pairs)
        )
        return to_post


def test_funnel_min_severity_drops_lower():
    app_cfg = SimpleNamespace(min_severity="medium", max_findings=None,
                              review_visible_lines=False)
    pairs = [
        _finding("nit", code="n"),
        _finding("low", code="l"),
        _finding("medium", code="m"),
        _finding("high", code="h"),
    ]
    out = _funnel(app_cfg, pairs)
    assert [f.severity for f, _ in out] == ["medium", "high"]


def test_funnel_min_severity_none_keeps_all():
    app_cfg = SimpleNamespace(min_severity=None, max_findings=None,
                              review_visible_lines=False)
    out = _funnel(app_cfg, [_finding("nit"), _finding("low")])
    assert len(out) == 2


def test_funnel_max_findings_keeps_highest_severity_then_confidence():
    app_cfg = SimpleNamespace(min_severity=None, max_findings=2,
                              review_visible_lines=False)
    pairs = [
        _finding("low", confidence="high", code="low-high"),
        _finding("high", confidence="low", code="high-low"),
        _finding("medium", confidence="high", code="med-high"),
        _finding("high", confidence="high", code="high-high"),
    ]
    out = _funnel(app_cfg, pairs)
    assert [f.code for f, _ in out] == ["high-high", "high-low"]


def test_funnel_max_findings_stable_for_ties():
    app_cfg = SimpleNamespace(min_severity=None, max_findings=2,
                              review_visible_lines=False)
    pairs = [_finding("low", code=f"c{i}") for i in range(4)]
    out = _funnel(app_cfg, pairs)
    assert [f.code for f, _ in out] == ["c0", "c1"]


def test_funnel_min_severity_then_max_findings_composed():
    app_cfg = SimpleNamespace(min_severity="medium", max_findings=1,
                              review_visible_lines=False)
    pairs = [
        _finding("nit", code="n"),
        _finding("medium", code="m"),
        _finding("high", code="h"),
    ]
    out = _funnel(app_cfg, pairs)
    assert [f.code for f, _ in out] == ["h"]


# ---------------------------------------------------------------------------
# custom_instructions in the review prompt supplement
# ---------------------------------------------------------------------------


def test_prompt_supplement_includes_operator_guidance():
    from code_review.orchestration.prompts import _format_review_prompt_supplement

    out = _format_review_prompt_supplement(
        context_brief=None,
        commit_messages=[],
        include_commit_messages=False,
        custom_instructions="Focus on security issues.",
    )
    assert "<operator_review_guidance>" in out
    assert "Focus on security issues." in out
    assert "cannot change the required JSON output format" in out


def test_prompt_supplement_no_guidance_when_unset_or_blank():
    from code_review.orchestration.prompts import _format_review_prompt_supplement

    for v in (None, "   "):
        out = _format_review_prompt_supplement(
            context_brief=None,
            commit_messages=[],
            include_commit_messages=False,
            custom_instructions=v,
        )
        assert out == ""


def test_app_config_custom_instructions_normalized(monkeypatch):
    monkeypatch.setenv("CODE_REVIEW_CUSTOM_INSTRUCTIONS", "   hello  ")
    assert CodeReviewAppConfig().custom_instructions == "hello"
    monkeypatch.setenv("CODE_REVIEW_CUSTOM_INSTRUCTIONS", "   ")
    assert CodeReviewAppConfig().custom_instructions is None


def test_app_config_custom_instructions_capped(monkeypatch, caplog):
    monkeypatch.setenv("CODE_REVIEW_CUSTOM_INSTRUCTIONS", "x" * 5000)
    with caplog.at_level("WARNING"):
        cfg = CodeReviewAppConfig()
    assert len(cfg.custom_instructions) == 4000
    assert "truncating" in caplog.text


def test_app_config_min_severity_and_max_findings_env(monkeypatch):
    monkeypatch.setenv("CODE_REVIEW_MIN_SEVERITY", "high")
    monkeypatch.setenv("CODE_REVIEW_MAX_FINDINGS", "5")
    cfg = CodeReviewAppConfig()
    assert cfg.min_severity == "high"
    assert cfg.max_findings == 5
    monkeypatch.setenv("CODE_REVIEW_MIN_SEVERITY", "nit")
    with pytest.raises(ValidationError):
        CodeReviewAppConfig()
    monkeypatch.setenv("CODE_REVIEW_MIN_SEVERITY", "low")
    monkeypatch.setenv("CODE_REVIEW_MAX_FINDINGS", "0")
    with pytest.raises(ValidationError):
        CodeReviewAppConfig()


# ---------------------------------------------------------------------------
# Public API + explicit app_config precedence
# ---------------------------------------------------------------------------


def test_public_api_exports_app_config_names():
    import code_review

    assert "CodeReviewAppConfig" in code_review.__all__
    assert "get_code_review_app_config" in code_review.__all__
    assert code_review.CodeReviewAppConfig is CodeReviewAppConfig
    assert code_review.get_code_review_app_config is not None


def test_orchestrator_app_config_overrides_env(monkeypatch):
    """An explicit app_config passed to ReviewOrchestrator wins over env config."""
    from code_review.orchestration.orchestrator import ReviewOrchestrator

    monkeypatch.setenv("CODE_REVIEW_MIN_SEVERITY", "high")
    override = CodeReviewAppConfig(min_severity="low")
    orch = ReviewOrchestrator("o", "r", 1, app_config=override)

    env_cfg = CodeReviewAppConfig()  # picks up env: high
    selected = orch._app_config_override or env_cfg
    assert selected.min_severity == "low"
    assert env_cfg.min_severity == "high"


# ---------------------------------------------------------------------------
# Stale-comment auto-resolve protections (keep_fingerprints / skip_paths)
# ---------------------------------------------------------------------------


def _resolvable_poster():
    from code_review.models import PRContext
    from code_review.orchestration.posting import CommentPoster

    provider = MagicMock()
    provider.capabilities.return_value.resolvable_comments = True
    poster = CommentPoster(provider, PRContext("o", "r", 1, "sha"))
    return poster, provider


def _existing_comment(cid: str, fingerprint: str, path: str = "a.py"):
    return SimpleNamespace(
        id=cid,
        path=path,
        body=f"<!-- code-review-agent:fingerprint={fingerprint};version=1 -->\n\nbody",
    )


def test_resolve_stale_keeps_fingerprint_of_capped_finding():
    """A finding dropped by min_severity/max_findings keeps its comment unresolved."""
    poster, provider = _resolvable_poster()
    existing = [_existing_comment("c-1", "fp-capped")]
    poster.resolve_stale(existing, [], dry_run=False,
                         keep_fingerprints={"fp-capped"})
    provider.resolve_comment.assert_not_called()


def test_resolve_stale_skips_unreviewed_paths():
    """Comments on files that produced no review (incomplete coverage) are kept."""
    poster, provider = _resolvable_poster()
    existing = [
        _existing_comment("c-1", "fp-old", path="skipped.py"),
        _existing_comment("c-2", "fp-gone", path="reviewed.py"),
    ]
    poster.resolve_stale(existing, [], dry_run=False,
                         skip_paths={"skipped.py"})
    provider.resolve_comment.assert_called_once_with("o", "r", "c-2", pr_number=1)


def test_resolve_stale_resolves_unprotected_comments():
    """Baseline: comments whose fingerprint is neither posted nor protected resolve."""
    poster, provider = _resolvable_poster()
    existing = [
        _existing_comment("c-1", "fp-stale"),
        _existing_comment("c-2", "fp-new"),
        _existing_comment("c-3", "fp-kept", path="u.py"),
    ]
    posted = [(_finding("low"), "fp-new")]
    poster.resolve_stale(
        existing, posted, dry_run=False,
        keep_fingerprints={"fp-kept"}, skip_paths={"other.py"},
    )
    provider.resolve_comment.assert_called_once_with("o", "r", "c-1", pr_number=1)
