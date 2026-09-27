"""Tests for wave-7 items: token counting, parallel SCM fetch, JSON logging,
Bitbucket no-labels warning, monorepo standards."""

from __future__ import annotations

import json
import logging
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_review import tokens as tokens_mod
from code_review.batching import build_review_batches, split_file_diff_into_segments
from code_review.diff.utils import estimate_tokens
from code_review.logging_config import JsonFormatter, configure_logging
from code_review.providers.base import FileInfo
from code_review.standards.detector import detect_review_contexts
from code_review.standards.prompts import get_review_standards, get_review_standards_multi
from code_review.tokens import count_tokens


@pytest.fixture(autouse=True)
def _reset_token_caches():
    tokens_mod._reset_caches_for_tests()
    yield
    tokens_mod._reset_caches_for_tests()


# --- count_tokens ---------------------------------------------------------


def test_count_tokens_uses_litellm_when_model_given():
    fake = SimpleNamespace(token_counter=lambda model, text: 7)
    with patch.dict(sys.modules, {"litellm": fake}):
        assert count_tokens("abcd" * 100, model="gpt-4o") == 7


def test_count_tokens_falls_back_without_model():
    text = "x" * 40
    assert count_tokens(text) == estimate_tokens(text)


def test_count_tokens_falls_back_when_litellm_missing():
    with patch.dict(sys.modules, {"litellm": None}):
        text = "x" * 40
        assert count_tokens(text, model="gpt-4o") == estimate_tokens(text)
    # After the failure the missing flag is cached — still falls back.
    assert count_tokens(text, model="gpt-4o") == estimate_tokens(text)


def test_count_tokens_caches_failing_model():
    calls = []

    def boom(model, text):
        calls.append(model)
        raise RuntimeError("no tokenizer")

    fake = SimpleNamespace(token_counter=boom)
    with patch.dict(sys.modules, {"litellm": fake}):
        assert count_tokens("x" * 40, model="weird-model") == estimate_tokens("x" * 40)
        # second call does not invoke litellm again
        assert count_tokens("y" * 40, model="weird-model") == estimate_tokens("y" * 40)
    assert calls == ["weird-model"]


def test_count_tokens_empty():
    assert count_tokens("", model="gpt-4o") == 0


# --- batching with injected token_counter ---------------------------------


def _diff(path: str, body: str = "+x") -> str:
    return "\n".join(
        [
            f"diff --git a/{path} b/{path}",
            f"--- a/{path}",
            f"+++ b/{path}",
            "@@ -1,1 +1,1 @@",
            body,
        ]
    ).strip()


def test_build_review_batches_uses_injected_counter():
    files = [FileInfo(path="a.py"), FileInfo(path="b.py")]
    diffs = {"a.py": _diff("a.py"), "b.py": _diff("b.py")}
    seen = []

    def counter(text: str) -> int:
        seen.append(text)
        return 1  # every estimate is tiny

    batches = build_review_batches(files, diffs, diff_budget_tokens=10, token_counter=counter)
    assert sum(len(b.segments) for b in batches) == 2
    assert seen  # counter actually used


def test_split_file_diff_segments_respect_counter():
    diff = _diff("big.py", "+line\n" * 40)
    counters = []

    def counter(text: str) -> int:
        counters.append(text)
        return len(text) // 10 + 1

    segments = split_file_diff_into_segments(
        "big.py", diff, segment_budget_tokens=5, token_counter=counter
    )
    assert len(segments) > 1
    assert all(s.estimated_tokens <= 8 for s in segments)


# --- parallel SCM fetch ---------------------------------------------------


def _provider_with_caps(concurrent: bool):
    p = MagicMock()
    p.capabilities.return_value = SimpleNamespace(
        supports_concurrent_fetches=concurrent
    )
    return p


def test_get_file_lines_parallel(monkeypatch):
    from code_review import orchestration_deps as deps

    monkeypatch.setenv("CODE_REVIEW_SCM_FETCH_CONCURRENCY", "8")
    provider = _provider_with_caps(concurrent=True)
    in_flight = [0]
    max_seen = [0]

    def fetch(owner, repo, ref, path):
        in_flight[0] += 1
        max_seen[0] = max(max_seen[0], in_flight[0])
        time.sleep(0.05)
        in_flight[0] -= 1
        return f"{path}:l1\n{path}:l2"

    provider.get_file_content.side_effect = fetch
    paths = [f"f{i}.py" for i in range(6)]
    out = deps._get_file_lines_by_path(provider, "o", "r", "sha", paths)
    assert out["f0.py"] == ["f0.py:l1", "f0.py:l2"]
    assert list(out) == paths  # order preserved
    assert max_seen[0] > 1  # actually concurrent


def test_get_file_lines_sequential_for_github(monkeypatch):
    from code_review import orchestration_deps as deps

    monkeypatch.setenv("CODE_REVIEW_SCM_FETCH_CONCURRENCY", "8")
    provider = _provider_with_caps(concurrent=False)
    provider.get_file_content.side_effect = lambda o, r, ref, p: "l"
    out = deps._get_file_lines_by_path(provider, "o", "r", "s", ["a", "b"])
    assert out == {"a": ["l"], "b": ["l"]}


def test_scm_fetch_concurrency_clamped_to_16(monkeypatch):
    """CODE_REVIEW_SCM_FETCH_CONCURRENCY is bounded — huge values can't
    stampede the SCM with an unbounded thread fan-out."""
    from code_review import orchestration_deps as deps

    provider = _provider_with_caps(concurrent=True)
    monkeypatch.setenv("CODE_REVIEW_SCM_FETCH_CONCURRENCY", "1000")
    assert deps._scm_fetch_concurrency(provider) == 16
    monkeypatch.setenv("CODE_REVIEW_SCM_FETCH_CONCURRENCY", "8")
    assert deps._scm_fetch_concurrency(provider) == 8
    monkeypatch.setenv("CODE_REVIEW_SCM_FETCH_CONCURRENCY", "16")
    assert deps._scm_fetch_concurrency(provider) == 16


def test_get_file_lines_error_per_path(monkeypatch):
    from code_review import orchestration_deps as deps

    monkeypatch.setenv("CODE_REVIEW_SCM_FETCH_CONCURRENCY", "4")
    provider = _provider_with_caps(concurrent=True)

    def fetch(o, r, ref, p):
        if p == "bad.py":
            raise RuntimeError("nope")
        return "line"

    provider.get_file_content.side_effect = fetch
    out = deps._get_file_lines_by_path(provider, "o", "r", "s", ["a.py", "bad.py"])
    assert out == {"a.py": ["line"], "bad.py": []}


# --- JSON logging ---------------------------------------------------------


def test_json_formatter_emits_object():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        "code_review.test", logging.WARNING, "f.py", 1, "hello %s", ("world",), None
    )
    record.trace_id = "abc123"
    record.custom_field = {"k": 1}
    record.not_serializable = object()
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "code_review.test"
    assert payload["message"] == "hello world"
    assert payload["trace_id"] == "abc123"
    assert payload["custom_field"] == {"k": 1}
    assert "not_serializable" not in payload
    assert payload["ts"]


def test_json_formatter_exc_info_and_default_trace():
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "x", logging.ERROR, "f.py", 1, "msg", (), sys.exc_info()
        )
    payload = json.loads(formatter.format(record))
    assert payload["trace_id"] == "-"
    assert "ValueError: boom" in payload["exc_info"]


def test_json_formatter_stringifies_non_text_trace_id():
    """A non-string trace_id (e.g. uuid4) must not break JSON serialisation."""
    import uuid

    formatter = JsonFormatter()
    record = logging.LogRecord("x", logging.INFO, "f.py", 1, "msg", (), None)
    record.trace_id = uuid.uuid4()
    payload = json.loads(formatter.format(record))
    assert isinstance(payload["trace_id"], str)
    assert uuid.UUID(payload["trace_id"])  # round-trips to a valid UUID string


def test_configure_logging_json(monkeypatch):
    monkeypatch.setenv("CODE_REVIEW_LOG_FORMAT", "json")
    monkeypatch.setenv("CODE_REVIEW_LOG_LEVEL", "INFO")
    log = logging.getLogger("code_review")
    saved_handlers = log.handlers[:]
    try:
        log.handlers.clear()
        configure_logging()
        assert any(isinstance(h.formatter, JsonFormatter) for h in log.handlers)
        log.handlers.clear()
        monkeypatch.delenv("CODE_REVIEW_LOG_FORMAT")
        configure_logging()
        assert not any(isinstance(h.formatter, JsonFormatter) for h in log.handlers)
    finally:
        log.handlers.clear()
        log.handlers.extend(saved_handlers)


# --- Bitbucket no-labels warning ------------------------------------------


def test_bitbucket_no_labels_warns_once(capsys):
    from code_review.orchestration.orchestrator import ReviewOrchestrator
    from code_review.schemas.findings import FindingV1  # noqa: F401

    orch = ReviewOrchestrator("o", "r", 1, "sha")
    provider = MagicMock()
    provider.capabilities.return_value = SimpleNamespace(supports_pr_labels=False)
    pr = SimpleNamespace(labels=[], title="x")
    provider.get_pr_info.return_value = pr
    cfg = SimpleNamespace(
        skip_label="skip-review",
        skip_labels=lambda: ["skip-review"],
        skip_title_pattern="",
    )
    obs = MagicMock()

    with patch("code_review.orchestration.orchestrator.runner_mod"):
        assert orch._skip_if_needed(provider, cfg, obs) is None
        assert orch._skip_if_needed(provider, cfg, obs) is None
    out = capsys.readouterr().out
    assert out.count("has no PR labels") == 1


def test_labelled_provider_no_warning(capsys):
    from code_review.orchestration.orchestrator import ReviewOrchestrator

    orch = ReviewOrchestrator("o", "r", 1, "sha")
    provider = MagicMock()
    provider.capabilities.return_value = SimpleNamespace(supports_pr_labels=True)
    provider.get_pr_info.return_value = SimpleNamespace(labels=["ok"], title="t")
    cfg = SimpleNamespace(
        skip_label="skip-review",
        skip_labels=lambda: ["skip-review"],
        skip_title_pattern="",
    )
    with patch("code_review.orchestration.orchestrator.runner_mod"):
        assert orch._skip_if_needed(provider, cfg, MagicMock()) is None
    assert "has no PR labels" not in capsys.readouterr().out


# --- monorepo standards ---------------------------------------------------


def test_detect_review_contexts_single_root():
    primary, contexts = detect_review_contexts(
        ["backend/package.json", "backend/app.py", "backend/lib.py"]
    )
    assert len(contexts) == 1
    assert primary.language == contexts[0].language


def test_detect_review_contexts_monorepo_union():
    paths = [
        "backend/go.mod",
        "backend/main.go",
        "backend/util.go",
        "frontend/package.json",
        "frontend/app.ts",
    ]
    primary, contexts = detect_review_contexts(paths)
    languages = {c.language for c in contexts}
    # go.mod → go root; package.json + a.ts → the frontend root (javascript
    # family — package.json's path signal is authoritative for the group).
    assert "go" in languages and languages & {"javascript", "typescript"}
    assert len(contexts) >= 2


def test_get_review_standards_multi_union():
    from code_review.standards.prompts import BASE_REVIEW_PROMPT

    primary, contexts = detect_review_contexts(
        ["backend/go.mod", "backend/a.go", "frontend/package.json", "frontend/a.ts"]
    )
    multi = get_review_standards_multi(contexts)
    single = get_review_standards(primary.language, primary.framework)
    assert BASE_REVIEW_PROMPT in multi
    assert multi.count(BASE_REVIEW_PROMPT[:40]) == 1
    # contains at least as much content as the single-language variant
    assert len(multi) >= len(single)


def test_get_review_standards_multi_single_is_identical():
    ctx = detect_review_contexts(["pkg/foo.py"])[1][0]
    assert get_review_standards_multi([ctx]) == get_review_standards(
        ctx.language, ctx.framework
    )
