"""Tests for runner findings parsing and ignore set."""

import pytest

from code_review.comments.manager import _build_ignore_set
from code_review.context.types import ContextReference, ReferenceType
from code_review.formatters.comment import finding_to_comment_body
from code_review.orchestration.execution import (
    findings_from_batch_responses,
    missing_batch_response_indexes,
)
from code_review.orchestration.prompts import _LINKED_CONTEXT_HEADER
from code_review.orchestration_deps import (
    _build_commit_messages_block,
    _findings_from_response,
    _format_review_prompt_supplement,
    _parse_findings_json,
)
from code_review.schemas.findings import FindingV1


def test_build_ignore_set_from_dicts():
    comments = [{"path": "a.py", "body": "hello"}, {"path": "b.py", "body": "world"}]
    s = _build_ignore_set(comments)
    assert len(s) == 2
    assert ("a.py", "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824") in s


def test_parse_findings_json_markdown_wrapped():
    text = (
        "```json\n"
        '{"findings":[{"path":"y","line":2,"severity":"medium","code":"s","message":"msg"}]}'
        "\n```"
    )
    out = _parse_findings_json(text)
    assert out["findings"][0]["path"] == "y"


def test_parse_findings_json_unlabeled_fence():
    text = (
        "```\n"
        '{"findings":[{"path":"u","line":5,"severity":"low","code":"s","message":"msg"}]}'
        "\n```"
    )
    out = _parse_findings_json(text)
    assert out["findings"][0]["path"] == "u"


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


def test_findings_from_response_invalid_schema_raises_value_error():
    """Schema-invalid JSON must raise when raise_errors=True so batch retry logic can recover."""
    text = '{"findings":[{"path":"p","line":1},{"not":"valid"}]}'
    with pytest.raises(ValueError, match="Failed to validate structured findings JSON"):
        _findings_from_response(text, raise_errors=True)


def test_findings_from_response_non_object_raises_value_error():
    """A top-level JSON array is malformed for the findings contract when raise_errors=True."""
    text = '[{"path":"p","line":1,"severity":"high","code":"x","message":"fix it"}]'
    with pytest.raises(
        ValueError, match="expected top-level object, got list"
    ):
        _findings_from_response(text, raise_errors=True)


def test_findings_from_response_malformed_json_raises_value_error():
    """Malformed JSON from agent must raise when raise_errors=True."""
    text = '{"path": "missing array wrapper"'  # invalid JSON
    with pytest.raises(ValueError, match="Failed to parse structured findings JSON"):
        _findings_from_response(text, raise_errors=True)


def test_findings_from_batch_responses_propagates_parse_failure():
    """Batch parsing must surface malformed responses by returning failed indexes."""
    responses = [("batch_review_0", '{"path": "missing array wrapper"')]
    findings, failed_indexes = findings_from_batch_responses(responses)
    assert len(findings) == 0
    assert failed_indexes == [0]


def test_findings_from_batch_responses_returns_failed_indexes_for_schema_errors():
    """Batch parsing must mark schema-invalid JSON for retry, not silently drop it."""
    responses = [("batch_review_0", '{"findings":[{"path":"p","line":1},{"not":"valid"}]}')]
    findings, failed_indexes = findings_from_batch_responses(responses)
    assert findings == []
    assert failed_indexes == [0]


def test_missing_batch_response_indexes_ignores_non_batch_authors():
    responses = [
        ("batch_review_1", '{"findings":[]}'),
        ("sequential_batch_review_agent", '{"findings":[]}'),
    ]

    assert missing_batch_response_indexes(responses, 3) == [0, 2]


def test_build_commit_messages_block_respects_remaining_char_budget():
    """A nearly-full prompt budget must not be exceeded by the next commit-message bullet."""
    header = "### PR commit messages (subject / first line)\n"
    max_chars = 60
    already_used_chars = max_chars - len(header) - 7

    block = _build_commit_messages_block(
        commit_messages=["A very long commit subject that should be truncated aggressively"],
        max_chars=max_chars,
        already_used_chars=already_used_chars,
    )

    assert block == header + "- A ve"
    assert already_used_chars + len(block) <= max_chars


def test_build_commit_messages_block_keeps_full_subject_when_unbounded():
    """Without a max_chars budget, reserve bullet/newline space before capping the subject."""
    subject = "x" * 600
    block = _build_commit_messages_block(
        commit_messages=[subject],
        max_chars=None,
        already_used_chars=0,
    )

    assert block.endswith("- " + ("x" * 497))


def test_format_review_prompt_supplement_wraps_linked_context_with_guidance():
    supplement = _format_review_prompt_supplement(
        context_brief="Must make DB optional.",
        context_references=[],
        commit_messages=[],
        include_commit_messages=False,
    )

    assert _LINKED_CONTEXT_HEADER in supplement
    assert "requirements, acceptance criteria, and constraints" in supplement
    assert "compare the diff against them" in supplement
    assert "first-class review findings" in supplement
    assert "Distilled brief:" in supplement
    assert "Must make DB optional." in supplement
    assert "<context>" not in supplement


def test_format_review_prompt_supplement_includes_linked_context_sources():
    supplement = _format_review_prompt_supplement(
        context_brief="Must enforce premium entitlement before export.",
        context_references=[
            ContextReference(
                ref_type=ReferenceType.JIRA,
                external_id="PAY-42",
                display="PAY-42",
            ),
            ContextReference(
                ref_type=ReferenceType.CONFLUENCE,
                external_id="123456",
                display="confluence-page:123456",
            ),
            ContextReference(
                ref_type=ReferenceType.GITHUB_ISSUE,
                external_id="org/repo#99",
                display="org/repo#99",
            ),
        ],
        commit_messages=[],
        include_commit_messages=False,
    )

    assert "Linked sources:" in supplement
    assert "- Jira: PAY-42" in supplement
    assert "- Confluence page: confluence-page:123456" in supplement
    assert "- GitHub issue: org/repo#99" in supplement
    assert "Must enforce premium entitlement before export." in supplement


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
