"""Tests for optional Prometheus/OpenTelemetry export (Phase 4.3)."""

import importlib

import pytest

import code_review.observability as observability_module
from code_review.observability import (
    RunHandle,
    finish_run,
    get_prometheus_registry,
    record_reply_dismissal_outcome,
    start_run,
)


def test_start_run_returns_handle_without_otel_env():
    """Without CODE_REVIEW_TRACING=otel, start_run returns a handle with no span."""
    handle = start_run("trace-123")
    assert isinstance(handle, RunHandle)
    assert handle.trace_id == "trace-123"
    # _span is None when OTel not enabled or not installed
    assert getattr(handle, "_span", None) is None or handle._span is None


def test_finish_run_no_op_without_prometheus_env():
    """finish_run does not raise when Prometheus is not enabled."""
    handle = start_run("trace-456")
    finish_run(
        handle,
        owner="o",
        repo="r",
        pr_number=1,
        files_count=2,
        findings_count=1,
        posts_count=1,
        duration_seconds=1.5,
    )


def test_get_prometheus_registry_none_without_env():
    """get_prometheus_registry returns None when CODE_REVIEW_METRICS not set."""
    try:
        reg = get_prometheus_registry()
        # Without CODE_REVIEW_METRICS=prometheus we expect None (or a Registry
        # if another test set it).
        assert reg is None or hasattr(reg, "register")
    except Exception:
        pytest.fail("get_prometheus_registry should not raise when observability deps missing")


def test_run_handle_end_with_no_span():
    """RunHandle.end() is safe when _span is None."""
    h = RunHandle(trace_id="x", _span=None)
    h.end(1.0, owner="o", repo="r", pr_number=1)


def test_record_reply_dismissal_outcome_no_op_without_prometheus():
    """record_reply_dismissal_outcome does not raise when metrics are disabled."""
    record_reply_dismissal_outcome("agreed")


def test_record_reply_dismissal_outcome_increments_when_prometheus_enabled(monkeypatch):
    pytest.importorskip("prometheus_client")
    from prometheus_client import generate_latest

    monkeypatch.setenv("CODE_REVIEW_METRICS", "prometheus")
    importlib.reload(observability_module)
    try:
        observability_module.record_reply_dismissal_outcome("agreed")
        observability_module.record_reply_dismissal_outcome("disagreed")
        reg = observability_module.get_prometheus_registry()
        assert reg is not None
        out = generate_latest(reg).decode()
        assert "code_review_reply_dismissal_total" in out
    finally:
        monkeypatch.delenv("CODE_REVIEW_METRICS", raising=False)
        importlib.reload(observability_module)
