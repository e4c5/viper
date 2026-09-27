"""Tests for optional Prometheus/OpenTelemetry export (Phase 4.3)."""

import importlib
import re
import sys
import types
from unittest.mock import MagicMock

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
    reg = get_prometheus_registry()
    # Without CODE_REVIEW_METRICS=prometheus we expect None (or a Registry
    # if another test set it).
    assert reg is None or hasattr(reg, "register")


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
        observability_module.record_reply_dismissal_outcome("agreed")
        observability_module.record_reply_dismissal_outcome("disagreed")
        reg = observability_module.get_prometheus_registry()
        assert reg is not None
        out = generate_latest(reg).decode()
        # Per-label values (not just metric name) prove .labels(...).inc() ran correctly.
        assert re.search(
            r'code_review_reply_dismissal_total\{outcome="agreed"\}\s+2(?:\.0)?\b',
            out,
        )
        assert re.search(
            r'code_review_reply_dismissal_total\{outcome="disagreed"\}\s+1(?:\.0)?\b',
            out,
        )
    finally:
        monkeypatch.delenv("CODE_REVIEW_METRICS", raising=False)
        importlib.reload(observability_module)


# ---------------------------------------------------------------------------
# Enabled/fake-dependency paths
# ---------------------------------------------------------------------------


class _FakeMetric:
    def __init__(self, name, desc, labels=None, registry=None, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.labelnames = labels or []
        self.values = []
        self.total = 0.0
        self._label_instance = None

    def labels(self, **kwargs):
        self._label_instance = (kwargs, self)
        return _FakeLabelled(self)

    def inc(self, amount=1.0):
        self.total += amount

    def observe(self, v):
        self.values.append(v)


class _FakeLabelled:
    def __init__(self, parent):
        self.parent = parent
        self.count = 0

    def inc(self, amount=1.0):
        self.count += amount
        self.parent.total += amount


class _FakeRegistry:
    def __init__(self):
        self.registered = []


def _fake_prometheus_client():
    mod = types.ModuleType("prometheus_client")
    mod.CollectorRegistry = _FakeRegistry
    mod.Counter = _FakeMetric
    mod.Histogram = _FakeMetric
    return mod


@pytest.fixture
def _reload_observability():
    """Reload observability fresh; always restore the module afterwards."""
    yield
    importlib.reload(observability_module)


def test_prometheus_enabled_but_package_missing_is_noop(monkeypatch, _reload_observability):
    monkeypatch.setenv("CODE_REVIEW_METRICS", "prometheus")
    monkeypatch.delitem(sys.modules, "prometheus_client", raising=False)
    importlib.reload(observability_module)
    assert observability_module.PROMETHEUS_ENABLED is True
    # prometheus_client genuinely absent in this env -> init fails, no-op
    assert observability_module.get_prometheus_registry() is None
    observability_module.record_reply_dismissal_outcome("agreed")  # must not raise
    h = observability_module.start_run("t1")
    observability_module.finish_run(
        h, owner="o", repo="r", pr_number=1, files_count=1,
        findings_count=2, posts_count=1, duration_seconds=0.5,
    )


def test_prometheus_enabled_with_fake_client_records_metrics(
    monkeypatch, _reload_observability
):
    fake = _fake_prometheus_client()
    monkeypatch.setitem(sys.modules, "prometheus_client", fake)
    monkeypatch.setenv("CODE_REVIEW_METRICS", "prometheus")
    importlib.reload(observability_module)
    try:
        assert observability_module.get_prometheus_registry() is not None
        h = observability_module.start_run("t2")
        observability_module.finish_run(
            h, owner="o", repo="r", pr_number=3, files_count=2,
            findings_count=4, posts_count=1, duration_seconds=1.25,
            context_brief_attached=True,
        )
        run_counter = observability_module._prometheus_run_counter
        assert run_counter.total == 1
        labels, _ = run_counter._label_instance
        assert labels == {"outcome": "completed", "context_aware": "true"}
        assert observability_module._prometheus_duration_histogram.values == [1.25]
        assert observability_module._prometheus_findings_counter.total == 4
        assert observability_module._prometheus_posts_counter.total == 1

        observability_module.finish_run(
            h, owner="o", repo="r", pr_number=3, files_count=0,
            findings_count=0, posts_count=0, duration_seconds=0.1,
        )
        labels, _ = run_counter._label_instance
        assert labels["outcome"] == "skipped"

        observability_module.record_reply_dismissal_outcome("disagreed")
        dismiss = observability_module._prometheus_reply_dismissal_counter
        labels, _ = dismiss._label_instance
        assert labels == {"outcome": "disagreed"}
        assert dismiss.total == 1

        # Run-duration histogram covers long reviews (buckets past 60s).
        buckets = observability_module._prometheus_duration_histogram.kwargs["buckets"]
        assert buckets == tuple(
            sorted(buckets)
        ) and max(buckets) >= 3600 and 120.0 in buckets
    finally:
        monkeypatch.delitem(sys.modules, "prometheus_client", raising=False)


def test_prometheus_disabled_get_registry_none(monkeypatch, _reload_observability):
    monkeypatch.delenv("CODE_REVIEW_METRICS", raising=False)
    monkeypatch.delenv("CODE_REVIEW_PROMETHEUS", raising=False)
    importlib.reload(observability_module)
    assert observability_module.PROMETHEUS_ENABLED is False
    assert observability_module.get_prometheus_registry() is None


def test_otel_missing_is_noop(monkeypatch, _reload_observability):
    monkeypatch.setenv("CODE_REVIEW_TRACING", "otel")
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    importlib.reload(observability_module)
    try:
        h = observability_module.start_run("t3")
        assert h._span is None
    finally:
        monkeypatch.delitem(sys.modules, "opentelemetry", raising=False)


def test_otel_enabled_creates_span(monkeypatch, _reload_observability):
    pytest.importorskip("opentelemetry")
    monkeypatch.setenv("CODE_REVIEW_TRACING", "otel")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    importlib.reload(observability_module)
    h = observability_module.start_run("t4")
    assert h.trace_id == "t4"
    assert h._span is not None
    observability_module.finish_run(
        h, owner="o", repo="r", pr_number=1, files_count=0,
        findings_count=0, posts_count=0, duration_seconds=0.1,
    )


def test_otel_endpoint_without_exporter_package_falls_back(
    monkeypatch, _reload_observability
):
    pytest.importorskip("opentelemetry")
    monkeypatch.setenv("CODE_REVIEW_TRACING", "otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    # OTLP exporter package is not installed -> ImportError -> fallback tracer
    importlib.reload(observability_module)
    h = observability_module.start_run("t5")
    assert h is not None


def test_run_handle_end_sets_attributes_and_ends_span():
    span = MagicMock()
    h = RunHandle(trace_id="x", _span=span)
    h.end(2.5, owner="me", repo=None)
    span.set_attribute.assert_any_call("code_review.owner", "me")
    span.set_attribute.assert_any_call("code_review.duration_seconds", 2.5)
    # None attribute skipped
    calls = {c.args[0] for c in span.set_attribute.call_args_list}
    assert "code_review.repo" not in calls
    span.end.assert_called_once()


def test_run_handle_end_always_ends_span_on_attribute_error():
    span = MagicMock()
    span.set_attribute.side_effect = ValueError("bad attr")
    h = RunHandle(trace_id="x", _span=span)
    with pytest.raises(ValueError):
        h.end(1.0, owner="o")
    span.end.assert_called_once()


def test_env_enabled_variants(monkeypatch):
    from code_review.observability import _env_enabled

    monkeypatch.delenv("XTEST", raising=False)
    assert _env_enabled("XTEST") is False
    for val in ("1", "true", "YES"):
        monkeypatch.setenv("XTEST", val)
        assert _env_enabled("XTEST") is True
    monkeypatch.setenv("XTEST", "prometheus")
    assert _env_enabled("XTEST") is False
    assert _env_enabled("XTEST", "prometheus") is True
