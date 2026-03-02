"""Tests for runner findings parsing and ignore set."""

from code_review.formatters.comment import finding_to_comment_body
from code_review.runner import (
    _build_ignore_set,
    _findings_from_response,
    _parse_findings_json,
)
from code_review.schemas.findings import FindingV1


def test_build_ignore_set_from_dicts():
    comments = [{"path": "a.py", "body": "hello"}, {"path": "b.py", "body": "world"}]
    s = _build_ignore_set(comments)
    assert len(s) == 2
    assert ("a.py", "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824") in s


def test_parse_findings_json_raw_array():
    text = '[{"path":"x","line":1,"severity":"info","code":"c","message":"m"}]'
    out = _parse_findings_json(text)
    assert len(out) == 1
    assert out[0]["path"] == "x" and out[0]["line"] == 1


def test_parse_findings_json_markdown_wrapped():
    text = '```json\n[{"path":"y","line":2,"severity":"suggestion","code":"s","message":"msg"}]\n```'
    out = _parse_findings_json(text)
    assert len(out) == 1
    assert out[0]["path"] == "y"


def test_findings_from_response_valid():
    text = '[{"path":"p","line":3,"severity":"critical","code":"x","message":"fix it"}]'
    findings = _findings_from_response(text)
    assert len(findings) == 1
    assert isinstance(findings[0], FindingV1)
    assert findings[0].severity == "critical"


def test_findings_from_response_invalid_skipped():
    text = '[{"path":"p","line":1},{"not":"valid"}]'
    findings = _findings_from_response(text)
    # First item missing required fields, second not a valid finding
    assert len(findings) == 0


def test_findings_from_response_malformed_json_returns_empty():
    """Malformed JSON from agent should not raise; runner parses it as no findings."""
    text = '{"path": "missing array wrapper"'  # invalid JSON
    findings = _findings_from_response(text)
    assert findings == []


def test_finding_to_comment_body():
    f = FindingV1(path="a.py", line=1, severity="suggestion", code="x", message="Do Y.")
    body = finding_to_comment_body(f)
    assert body == "[Suggestion] Do Y."
