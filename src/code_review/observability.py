"""
Optional Prometheus and OpenTelemetry export for run_review (Phase 4.3).

Enable via environment:
- Prometheus: CODE_REVIEW_METRICS=prometheus (or CODE_REVIEW_PROMETHEUS=1)
- OpenTelemetry: CODE_REVIEW_TRACING=otel (or CODE_REVIEW_OTEL=1)

Requires optional dependencies: pip install -e ".[observability]"
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Env flags: accept "1", "true", "yes", "prometheus" / "otel"
def _env_enabled(name: str, value_ok: str | None = None) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return False
    if value_ok:
        return v in ("1", "true", "yes", value_ok)
    return v in ("1", "true", "yes")


PROMETHEUS_ENABLED = _env_enabled("CODE_REVIEW_METRICS", "prometheus") or _env_enabled(
    "CODE_REVIEW_PROMETHEUS"
)
OTEL_ENABLED = _env_enabled("CODE_REVIEW_TRACING", "otel") or _env_enabled("CODE_REVIEW_OTEL")

# Lazy refs to optional libs
_prometheus_registry: Any = None
_prometheus_run_counter: Any = None
_prometheus_duration_histogram: Any = None
_prometheus_findings_counter: Any = None
_prometheus_posts_counter: Any = None
_prometheus_reply_dismissal_counter: Any = None
_otel_tracer: Any = None
_otel_tracer_provider: Any = None


def _init_prometheus() -> bool:
    global _prometheus_registry, _prometheus_run_counter, _prometheus_duration_histogram
    global _prometheus_findings_counter, _prometheus_posts_counter
    global _prometheus_reply_dismissal_counter
    if _prometheus_registry is not None:
        return _prometheus_registry is not False
    if not PROMETHEUS_ENABLED:
        _prometheus_registry = False
        return False
    try:
        from prometheus_client import CollectorRegistry, Counter, Histogram

        _prometheus_registry = CollectorRegistry()
        _prometheus_run_counter = Counter(
            "code_review_runs_total",
            "Total code review runs",
            ["outcome", "context_aware"],
            registry=_prometheus_registry,
        )
        _prometheus_duration_histogram = Histogram(
            "code_review_run_duration_seconds",
            "Run duration in seconds",
            buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
            registry=_prometheus_registry,
        )
        _prometheus_findings_counter = Counter(
            "code_review_findings_total",
            "Total findings from reviews",
            registry=_prometheus_registry,
        )
        _prometheus_posts_counter = Counter(
            "code_review_posts_total",
            "Total comments posted",
            registry=_prometheus_registry,
        )
        _prometheus_reply_dismissal_counter = Counter(
            "code_review_reply_dismissal_total",
            "Reply-dismissal outcomes in review-decision-only runs",
            ["outcome"],
            registry=_prometheus_registry,
        )
        return True
    except ImportError:
        logger.debug("Prometheus export requested but prometheus_client not installed")
        _prometheus_registry = False
        return False


def _init_otel() -> bool:
    global _otel_tracer, _otel_tracer_provider
    if _otel_tracer is not None:
        return _otel_tracer is not False
    if not OTEL_ENABLED:
        _otel_tracer = False
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
        )
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                current = trace.get_tracer_provider()
                # Only set our provider when no application-level provider exists
                # (i.e. current is the default proxy or no-op, not an SDK/app provider).
                _cls = type(current)
                _is_default = _cls.__name__ in (
                    "ProxyTracerProvider",
                    "NoOpTracerProvider",
                    "_DefaultTracerProvider",
                ) and (_cls.__module__ or "").startswith("opentelemetry.trace")
                if _is_default:
                    exporter = OTLPSpanExporter(endpoint=endpoint)
                    _otel_tracer_provider = TracerProvider()
                    _otel_tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
                    trace.set_tracer_provider(_otel_tracer_provider)
            except ImportError:
                pass
        # Use global provider (ours or app's); get_tracer works with no-op if none set
        _otel_tracer = trace.get_tracer("code-review-agent", "0.1.0")
        return True
    except ImportError:
        logger.debug("OpenTelemetry export requested but opentelemetry not installed")
        _otel_tracer = False
        return False


@dataclass
class RunHandle:
    """Handle for a run_review span; used to end the span with attributes."""

    trace_id: str
    _span: Any = None

    def end(self, duration_seconds: float, **attrs: Any) -> None:
        if self._span is not None:
            try:
                for k, v in attrs.items():
                    if v is not None:
                        self._span.set_attribute(f"code_review.{k}", v)
                self._span.set_attribute("code_review.duration_seconds", duration_seconds)
            finally:
                self._span.end()


def start_run(trace_id: str) -> RunHandle:
    """Start an observability run (OTel span if enabled). Call finish_run before return."""
    handle = RunHandle(trace_id=trace_id)
    if _init_otel():
        try:
            handle._span = _otel_tracer.start_span(
                "run_review",
                attributes={
                    "code_review.trace_id": trace_id,
                },
            )
        except Exception as e:
            logger.debug("OTel start_span failed: %s", e)
    return handle


def finish_run(
    handle: RunHandle,
    owner: str,
    repo: str,
    pr_number: int,
    files_count: int,
    findings_count: int,
    posts_count: int,
    duration_seconds: float,
    context_brief_attached: bool = False,
) -> None:
    """End the run span and record Prometheus metrics."""

    # End OTel span with attributes
    handle.end(
        duration_seconds,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        files_count=files_count,
        findings_count=findings_count,
        posts_count=posts_count,
        context_aware=context_brief_attached,
    )

    # Prometheus
    if _init_prometheus():
        try:
            outcome = "completed" if (files_count > 0 or findings_count > 0) else "skipped"
            context_aware = "true" if context_brief_attached else "false"
            _prometheus_run_counter.labels(outcome=outcome, context_aware=context_aware).inc()
            _prometheus_duration_histogram.observe(duration_seconds)
            _prometheus_findings_counter.inc(findings_count)
            _prometheus_posts_counter.inc(posts_count)
        except Exception as e:
            logger.debug("Prometheus record failed: %s", e)


def get_prometheus_registry():
    """Return the Prometheus Registry if metrics are enabled, else None. For /metrics endpoint."""
    if not PROMETHEUS_ENABLED:
        return None
    _init_prometheus()
    return _prometheus_registry if _prometheus_registry not in (None, False) else None


def record_reply_dismissal_outcome(outcome: str) -> None:
    """Increment reply-dismissal counter when Prometheus is enabled (no-op otherwise).

    ``outcome`` is a stable label: agreed, disagreed, parse_failed, llm_error,
    skipped_no_capability, skipped_insufficient_thread.
    """
    if not _init_prometheus():
        return
    try:
        _prometheus_reply_dismissal_counter.labels(outcome=outcome).inc()
    except Exception as e:
        logger.debug("Prometheus reply-dismissal record failed: %s", e)
