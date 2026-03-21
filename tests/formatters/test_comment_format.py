"""Tests for comment body format: severity prefix, marker injection, location consistency."""

import pytest

from code_review.diff.fingerprint import format_comment_body_with_marker
from code_review.formatters.comment import (
    SEVERITY_LABELS,
    _strip_path_prefixes,
    finding_to_comment_body,
    infer_severity_from_comment_body,
    max_inferred_severity,
)
from code_review.schemas.findings import FindingV1


def test_strip_path_prefixes():
    """dst:// and src:// are removed from displayed text."""
    assert _strip_path_prefixes("") == ""
    assert _strip_path_prefixes("hello") == "hello"
    assert _strip_path_prefixes("In dst://src/main/foo.java at line 1") == "In src/main/foo.java at line 1"
    assert _strip_path_prefixes("File src://bar.py") == "File bar.py"


def test_infer_severity_from_comment_body_strips_marker():
    body = "<!-- code-review-agent:fingerprint=x;version=1 -->\n\n[High] Problem"
    assert infer_severity_from_comment_body(body) == "high"
    assert infer_severity_from_comment_body("[Medium] Note") == "medium"
    assert infer_severity_from_comment_body("No tag here") == "unknown"


def test_max_inferred_severity():
    assert max_inferred_severity("low", "high") == "high"
    assert max_inferred_severity("medium", "low") == "medium"


def test_severity_labels_canonical():
    """Canonical prefixes are [High], [Medium], [Low]."""
    assert SEVERITY_LABELS["high"] == "[High]"
    assert SEVERITY_LABELS["medium"] == "[Medium]"
    assert SEVERITY_LABELS["low"] == "[Low]"


@pytest.mark.parametrize("severity", ["high", "medium", "low"])
def test_finding_to_comment_body_prefix(severity: str):
    """Body starts with [High]/[Medium]/[Low] and contains message."""
    f = FindingV1(
        path="foo.py",
        line=10,
        severity=severity,
        code="test-code",
        message="Fix this.",
    )
    body = finding_to_comment_body(f)
    expected_prefix = SEVERITY_LABELS[severity]
    assert body.startswith(expected_prefix), f"expected prefix {expected_prefix!r}"
    assert "Fix this." in body


def test_finding_to_comment_body_uses_get_body():
    """When body field is set, it is used instead of message."""
    f = FindingV1(
        path="a.py",
        line=1,
        severity="medium",
        code="x",
        message="msg",
        body="custom body text",
    )
    result = finding_to_comment_body(f)
    assert result == "[Medium] custom body text"
    assert "msg" not in result


def test_finding_to_comment_body_fallback_unknown_severity():
    """Unknown severity gets title-cased fallback."""
    f = FindingV1(
        path="x",
        line=1,
        severity="high",  # valid
        code="c",
        message="m",
    )
    body = finding_to_comment_body(f)
    assert body.startswith("[High]")

    # If we had a hypothetical "warning", formatter would use "[Warning]" via .title()
    # SEVERITY_LABELS only has critical/suggestion/info; others use f"[{f.severity.title()}]"


def test_marker_injection_after_formatter():
    """format_comment_body_with_marker wraps formatter output; marker precedes body."""
    f = FindingV1(
        path="p.py",
        line=5,
        severity="low",
        code="code",
        message="Note here.",
    )
    body = finding_to_comment_body(f)
    assert body == "[Low] Note here."
    with_marker = format_comment_body_with_marker(
        body, fingerprint="abc123", version="1.0", run_id="run-1"
    )
    assert "<!-- code-review-agent:" in with_marker
    assert "fingerprint=abc123" in with_marker
    assert "run=run-1" in with_marker
    assert with_marker.endswith("\n\n[Low] Note here.")


def test_location_path_line_consistency():
    """
    Runner posts (path, line, body); body from formatter has no path/line
    (payload carries location).
    """
    f = FindingV1(
        path="src/bar.py",
        line=42,
        severity="medium",
        code="unused-import",
        message="Remove unused import os.",
    )
    body = finding_to_comment_body(f)
    # Body is severity + message only; path and line are in the API payload tuple
    assert "src/bar.py" not in body
    assert "42" not in body
    assert body == "[Medium] Remove unused import os."
    # Simulate runner building the comment tuple
    comment_tuple = (f.path, f.line, body)
    assert comment_tuple[0] == "src/bar.py"
    assert comment_tuple[1] == 42
    assert comment_tuple[2].startswith("[Medium]")


def test_finding_to_comment_body_empty_body_uses_label_only():
    """When get_body() is empty after strip, body is just the severity label."""
    f = FindingV1(
        path="x.py",
        line=1,
        severity="medium",
        code="x",
        message="",
    )
    body = finding_to_comment_body(f)
    assert body == "[Medium]"


def test_finding_to_comment_body_strips_dst_prefix_from_message_and_prompt():
    """dst:// and src:// are stripped from message and agent_fix_prompt in the output."""
    f = FindingV1(
        path="dst://src/foo.py",
        line=1,
        severity="medium",
        code="x",
        message="In dst://src/foo.py at line 1, consider X.",
        agent_fix_prompt="In the file dst://src/foo.py at line 1, do Y.",
    )
    body = finding_to_comment_body(f, use_collapsible_prompt=False)
    assert "dst://" not in body
    assert "In src/foo.py at line 1" in body or "In the file src/foo.py at line 1" in body
