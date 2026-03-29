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


def test_parse_findings_json_markdown_wrapped():
    text = '```json\n{"findings":[{"path":"y","line":2,"severity":"medium","code":"s","message":"msg"}]}\n```'
    out = _parse_findings_json(text)
    assert out["findings"][0]["path"] == "y"


def test_parse_findings_json_structured_object():
    text = (
        '{"findings":[{"path":"z","line":4,"severity":"low","code":"c","message":"m"}]}'
    )
    out = _parse_findings_json(text)
    assert out["findings"][0]["path"] == "z"


def test_findings_from_response_valid():
    text = '{"findings":[{"path":"p","line":3,"severity":"high","code":"x","message":"fix it"}]}'
    findings = _findings_from_response(text)
    assert len(findings) == 1
    assert isinstance(findings[0], FindingV1)
    assert findings[0].severity == "high"


def test_findings_from_response_invalid_skipped():
    text = '{"findings":[{"path":"p","line":1},{"not":"valid"}]}'
    findings = _findings_from_response(text)
    # Invalid structured batches fail closed.
    assert len(findings) == 0


def test_findings_from_response_malformed_json_returns_empty():
    """Malformed JSON from agent should not raise; runner parses it as no findings."""
    text = '{"path": "missing array wrapper"'  # invalid JSON
    findings = _findings_from_response(text)
    assert findings == []


def test_finding_to_comment_body():
    f = FindingV1(path="a.py", line=1, severity="medium", code="x", message="Do Y.")
    body = finding_to_comment_body(f)
    assert body == "[Medium] Do Y."


def test_finding_to_comment_body_includes_agent_fix_prompt_in_collapsible_block():
    f = FindingV1(
        path="a.py",
        line=1,
        severity="medium",
        code="x",
        message="Do Y.",
        agent_fix_prompt="Verify Y and apply fix.",
    )
    body = finding_to_comment_body(f)
    assert body.startswith("[Medium] Do Y.")
    assert "<details>" in body
    assert "<summary>Prompt for AI Agents</summary>" in body
    assert "Verify Y and apply fix." in body
    assert body.strip().endswith("</details>")


def test_finding_to_comment_body_plain_prompt_when_not_collapsible():
    """When use_collapsible_prompt=False (e.g. Bitbucket), prompt is plain text, no HTML tags."""
    f = FindingV1(
        path="a.py",
        line=1,
        severity="high",
        code="x",
        message="Do Y.",
        agent_fix_prompt="Verify Y and apply fix.",
    )
    body = finding_to_comment_body(f, use_collapsible_prompt=False)
    assert body.startswith("[High] Do Y.")
    assert "<details>" not in body
    assert "<summary>" not in body
    assert "**Prompt for AI Agents**" in body
    assert "Verify Y and apply fix." in body
