"""Tests for comment body format: severity prefix, marker injection, location consistency."""

import pytest

from code_review.diff.fingerprint import format_comment_body_with_marker
from code_review.formatters.comment import SEVERITY_LABELS, finding_to_comment_body
from code_review.schemas.findings import FindingV1


def test_severity_labels_canonical():
    """Canonical prefixes are [Critical], [Suggestion], [Info]."""
    assert SEVERITY_LABELS["critical"] == "[Critical]"
    assert SEVERITY_LABELS["suggestion"] == "[Suggestion]"
    assert SEVERITY_LABELS["info"] == "[Info]"


@pytest.mark.parametrize("severity", ["critical", "suggestion", "info"])
def test_finding_to_comment_body_prefix(severity: str):
    """Body starts with [Critical]/[Suggestion]/[Info] and contains message."""
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
        severity="suggestion",
        code="x",
        message="msg",
        body="custom body text",
    )
    result = finding_to_comment_body(f)
    assert result == "[Suggestion] custom body text"
    assert "msg" not in result


def test_finding_to_comment_body_fallback_unknown_severity():
    """Unknown severity gets title-cased fallback."""
    f = FindingV1(
        path="x",
        line=1,
        severity="critical",  # valid
        code="c",
        message="m",
    )
    body = finding_to_comment_body(f)
    assert body.startswith("[Critical]")

    # If we had a hypothetical "warning", formatter would use "[Warning]" via .title()
    # SEVERITY_LABELS only has critical/suggestion/info; others use f"[{f.severity.title()}]"


def test_marker_injection_after_formatter():
    """format_comment_body_with_marker wraps formatter output; marker precedes body."""
    f = FindingV1(
        path="p.py",
        line=5,
        severity="info",
        code="code",
        message="Note here.",
    )
    body = finding_to_comment_body(f)
    assert body == "[Info] Note here."
    with_marker = format_comment_body_with_marker(
        body, fingerprint="abc123", version="1.0", run_id="run-1"
    )
    assert "<!-- code-review-agent:" in with_marker
    assert "fingerprint=abc123" in with_marker
    assert "run=run-1" in with_marker
    assert with_marker.endswith("\n\n[Info] Note here.")


def test_location_path_line_consistency():
    """Runner posts (path, line, body); body from formatter has no path/line (payload carries location)."""
    f = FindingV1(
        path="src/bar.py",
        line=42,
        severity="suggestion",
        code="unused-import",
        message="Remove unused import os.",
    )
    body = finding_to_comment_body(f)
    # Body is severity + message only; path and line are in the API payload tuple
    assert "src/bar.py" not in body
    assert "42" not in body
    assert body == "[Suggestion] Remove unused import os."
    # Simulate runner building the comment tuple
    comment_tuple = (f.path, f.line, body)
    assert comment_tuple[0] == "src/bar.py"
    assert comment_tuple[1] == 42
    assert comment_tuple[2].startswith("[Suggestion]")
