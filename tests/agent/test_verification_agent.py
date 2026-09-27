"""Tests for verification agent factory."""

import json as _json
from unittest.mock import MagicMock, patch

import pytest

from code_review.agent import verification_agent as va
from code_review.agent.verification_agent import (
    _build_verification_prompt,
    _extract_snippet_from_annotated,
    _parse_diff_header_path,
    _parse_verification_result,
    _run_verification_agent,
    _VerificationResult,
    _verify_batch,
    create_verification_agent,
    verify_findings,
)
from code_review.schemas.findings import FindingV1


@patch("code_review.models.get_configured_verification_model")
@patch("code_review.config.get_verification_llm_config")
@patch("code_review.config.get_llm_config")
@patch("google.adk.agents.Agent")
def test_create_verification_agent_uses_verification_model_helper(
    mock_agent_cls, mock_get_llm_cfg, mock_get_verification_cfg, mock_get_verification_model
):
    mock_get_llm_cfg.return_value = MagicMock(
        provider="gemini", model="gemini-3.1", max_output_tokens=8192
    )
    mock_get_verification_cfg.return_value = MagicMock(provider=None, model=None)
    mock_get_verification_model.return_value = "cheap-verification-model"
    inst = MagicMock()
    mock_agent_cls.return_value = inst

    out = create_verification_agent()

    assert out is inst
    _, kwargs = mock_agent_cls.call_args
    assert kwargs["model"] == "cheap-verification-model"
    assert kwargs["name"] == "verification_agent"
    assert kwargs["generate_content_config"].temperature == pytest.approx(0.1)
    mock_get_verification_model.assert_called_once()


@patch("code_review.agent.verification_agent.log_adk_llm_usage")
@patch("code_review.models.get_configured_verification_model")
@patch("code_review.config.get_verification_llm_config")
@patch("code_review.config.get_llm_config")
@patch("google.adk.agents.Agent")
def test_create_verification_agent_after_model_callback_accepts_adk_keywords(
    mock_agent_cls,
    mock_get_llm_cfg,
    mock_get_verification_cfg,
    mock_get_verification_model,
    mock_log_usage,
):
    mock_get_llm_cfg.return_value = MagicMock(
        provider="gemini", model="gemini-3.1", max_output_tokens=8192
    )
    mock_get_verification_cfg.return_value = MagicMock(provider=None, model=None)
    mock_get_verification_model.return_value = "cheap-verification-model"

    create_verification_agent()

    _, kwargs = mock_agent_cls.call_args
    response = MagicMock()
    kwargs["after_model_callback"](callback_context=MagicMock(), llm_response=response)

    mock_log_usage.assert_called_once()
    _, log_kwargs = mock_log_usage.call_args
    assert log_kwargs["task"] == "verification"
    assert log_kwargs["response"] is response
    assert log_kwargs["provider"] == "gemini"
    assert log_kwargs["model"] == "gemini-3.1"


@patch("code_review.models.get_configured_verification_model")
@patch("code_review.config.get_verification_llm_config")
@patch("code_review.config.get_llm_config")
@patch("google.adk.agents.Agent")
def test_create_verification_agent_omits_temperature_for_fixed_temperature_override(
    mock_agent_cls, mock_get_llm_cfg, mock_get_verification_cfg, mock_get_verification_model
):
    mock_get_llm_cfg.return_value = MagicMock(
        provider="gemini", model="gemini-3.1", max_output_tokens=8192
    )
    mock_get_verification_cfg.return_value = MagicMock(provider="openai", model="gpt-5.4")
    mock_get_verification_model.return_value = "openai/gpt-5.4"

    create_verification_agent()

    _, kwargs = mock_agent_cls.call_args
    assert kwargs["generate_content_config"].temperature is None


# ---------------------------------------------------------------------------
# verify_findings / _verify_batch / parsing (no real LLM)
# ---------------------------------------------------------------------------


def _finding(path="src/a.py", line=10, confidence=None, code="x-1", severity="medium"):
    return FindingV1(
        path=path, line=line, severity=severity, code=code,
        message="msg", confidence=confidence,
    )


def test_verify_findings_empty_returns_input():
    assert verify_findings([], "diff") == []


def test_verify_findings_all_high_passthrough(caplog):
    findings = [_finding(confidence="high"), _finding(confidence=None)]
    out = verify_findings(findings, "diff")
    assert out == findings


def test_verify_findings_merges_in_original_order(monkeypatch):
    f0 = _finding(confidence="high", code="c0")
    f1 = _finding(confidence="low", code="c1")
    f2 = _finding(confidence="medium", code="c2")
    f3 = _finding(confidence=None, code="c3")

    def fake_batch(batch, diff_text):
        # confirm only the second item in the batch
        return [(batch[1][0], batch[1][1])], [], 1

    monkeypatch.setattr(va, "_verify_batch", fake_batch)
    out = verify_findings([f0, f1, f2, f3], "diff")
    assert [f.code for f in out] == ["c0", "c2", "c3"]


def test_verify_findings_batches_over_max(monkeypatch):
    monkeypatch.setattr(va, "_MAX_FINDINGS_PER_BATCH", 2)
    calls = []

    def fake_batch(batch, diff_text):
        calls.append(len(batch))
        return list(batch), [], 0

    monkeypatch.setattr(va, "_verify_batch", fake_batch)
    out = verify_findings([_finding(confidence="low") for _ in range(5)], "d")
    assert calls == [2, 2, 1]
    assert len(out) == 5


def test_verify_batch_agent_failure_keeps_all_fail_open(monkeypatch):
    monkeypatch.setattr(va, "_run_verification_agent", lambda prompt: None)
    batch = [(0, _finding(confidence="low")), (2, _finding(confidence="medium"))]
    confirmed, fail_open, rejected = _verify_batch(batch, "diff")
    assert confirmed == []
    assert fail_open == batch
    assert rejected == 0


def test_verify_batch_confirm_reject_missing_verdict(monkeypatch):
    result = _VerificationResult.model_validate(
        {
            "verdicts": [
                {"index": 0, "verdict": "confirm", "reason": "real"},
                {"index": 1, "verdict": "reject", "reason": "fine"},
                # index 2 omitted -> fail-open
            ]
        }
    )
    monkeypatch.setattr(va, "_run_verification_agent", lambda prompt: result)
    batch = [
        (10, _finding(confidence="low", code="a")),
        (11, _finding(confidence="low", code="b")),
        (12, _finding(confidence="medium", code="c")),
    ]
    confirmed, fail_open, rejected = _verify_batch(batch, "diff")
    assert [f.code for _, f in confirmed] == ["a"]
    assert [f.code for _, f in fail_open] == ["c"]
    assert rejected == 1


def test_parse_verification_result_valid_json():
    text = _json.dumps(
        {"verdicts": [{"index": 0, "verdict": "confirm", "reason": "ok"}]}
    )
    out = _parse_verification_result(text)
    assert out is not None
    assert out.verdicts[0].verdict == "confirm"


def test_parse_verification_result_json_in_fenced_block():
    text = 'Here is my answer:\n```json\n{"verdicts": []}\n```\n— done.'
    out = _parse_verification_result(text)
    assert out is not None
    assert out.verdicts == []


def test_parse_verification_result_malformed_returns_none(caplog):
    assert _parse_verification_result("not json at all") is None


def test_parse_verification_result_wrong_schema_returns_none():
    assert _parse_verification_result('{"verdicts": [{"index": "x"}]}') is None


def test_parse_diff_header_path_variants():
    assert _parse_diff_header_path("diff --git a/foo.py b/foo.py") == "foo.py"
    assert (
        _parse_diff_header_path('diff --git "a/dir with space/f.py" "b/dir with space/f.py"')
        == "dir with space/f.py"
    )
    assert _parse_diff_header_path("garbage") == ""


def _sample_diff():
    return (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "+line2 changed\n"
        " line3\n"
        "diff --git a/src/other.py b/src/other.py\n"
        "--- a/src/other.py\n"
        "+++ b/src/other.py\n"
        "@@ -1,2 +1,2 @@\n"
        " x\n"
        "+y\n"
    )


def test_extract_snippet_no_diff_or_path():
    f = _finding(path="", line=1)
    assert _extract_snippet_from_annotated(f, "diff --git x", frozenset({"x"})) == ""
    f2 = _finding()
    assert _extract_snippet_from_annotated(f2, "", frozenset()) == ""
    # path not present in diff
    assert (
        _extract_snippet_from_annotated(f2, "diff --git a/z b/z\n", frozenset({"z"}))
        == ""
    )


def test_extract_snippet_collects_window_lines():
    from code_review.diff.parser import annotate_diff_with_line_numbers, parse_unified_diff
    from code_review.diff.utils import normalize_path

    diff = _sample_diff()
    annotated = annotate_diff_with_line_numbers(diff)
    paths = frozenset(normalize_path(h.path) for h in parse_unified_diff(diff))
    f = _finding(path="src/a.py", line=2)
    snippet = _extract_snippet_from_annotated(f, annotated, paths, radius=2)
    assert snippet
    assert "other.py" not in snippet


def test_build_verification_prompt_contains_finding_fields():
    batch = [(0, _finding(path="src/a.py", line=2, severity="high"))]
    prompt = _build_verification_prompt(batch, _sample_diff())
    assert "--- Finding 0 ---" in prompt
    assert "file: src/a.py" in prompt
    assert "severity: high" in prompt


def test_run_verification_agent_success(monkeypatch):
    monkeypatch.setattr(
        "code_review.orchestration.runner_utils._run_agent_and_collect_response",
        lambda runner, session_id, content: '{"verdicts": []}',
    )
    monkeypatch.setattr(
        "code_review.adk_runner.create_runner", lambda **kw: MagicMock()
    )
    monkeypatch.setattr(va, "create_verification_agent", lambda: MagicMock())
    out = _run_verification_agent("prompt")
    assert isinstance(out, _VerificationResult)


def test_run_verification_agent_empty_response_returns_none(monkeypatch):
    monkeypatch.setattr(
        "code_review.orchestration.runner_utils._run_agent_and_collect_response",
        lambda runner, session_id, content: "   ",
    )
    monkeypatch.setattr(
        "code_review.adk_runner.create_runner", lambda **kw: MagicMock()
    )
    monkeypatch.setattr(va, "create_verification_agent", lambda: MagicMock())
    assert _run_verification_agent("prompt") is None


def test_run_verification_agent_exception_returns_none(monkeypatch):
    monkeypatch.setattr(
        va, "create_verification_agent", MagicMock(side_effect=RuntimeError("boom"))
    )
    assert _run_verification_agent("prompt") is None


def test_create_verification_agent_prefers_verification_overrides():
    with (
        patch("code_review.models.get_configured_verification_model") as m_model,
        patch("code_review.config.get_verification_llm_config") as m_ver,
        patch("code_review.config.get_llm_config") as m_llm,
        patch("code_review.agent.verification_agent.log_adk_llm_usage") as m_log,
        patch("google.adk.agents.Agent") as m_agent,
    ):
        m_llm.return_value = MagicMock(
            provider="gemini", model="g-main", max_output_tokens=100
        )
        m_ver.return_value = MagicMock(provider="openai", model="gpt-x")
        m_model.return_value = "openai/gpt-x"
        create_verification_agent()
        _, kwargs = m_agent.call_args
        kwargs["after_model_callback"](callback_context=MagicMock(), llm_response=MagicMock())
        _, log_kwargs = m_log.call_args
        assert log_kwargs["provider"] == "openai"
        assert log_kwargs["model"] == "gpt-x"
        # max_output_tokens capped at 4096
        assert kwargs["generate_content_config"].max_output_tokens == 100
