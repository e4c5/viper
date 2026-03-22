"""Tests for dropping findings whose messages retract or negate the issue."""

from code_review.runner import (
    _filter_self_retracted_finding_messages,
    _finding_message_looks_self_retracted,
)
from code_review.schemas.findings import FindingV1


def _f(msg: str) -> FindingV1:
    return FindingV1(
        path="src/Foo.java",
        line=1,
        severity="medium",
        code="test",
        message=msg,
    )


def test_self_retracted_patterns_match_user_style_message():
    msg = (
        "The ValidationResult constructor is called with errors.isEmpty(). "
        "Actually, this is correct. I will retract this finding."
    )
    assert _finding_message_looks_self_retracted(msg) is True


def test_self_retracted_false_positive():
    assert _finding_message_looks_self_retracted("Possible NPE — false positive, guarded above.") is True


def test_legitimate_retract_in_domain_language_not_dropped():
    assert _finding_message_looks_self_retracted("Ensure the latch retracts before the door closes.") is False


def test_filter_drops_retracted_keeps_normal():
    good = _f("Constructor should validate errors list is non-null.")
    bad = _f("Wait, actually fine. I will retract this finding.")
    out = _filter_self_retracted_finding_messages([good, bad])
    assert len(out) == 1
    assert out[0].message == good.message


def test_agent_instructions_discourage_self_retracting_messages():
    from code_review.agent.agent import FINDINGS_ONLY_INSTRUCTION, SINGLE_SHOT_INSTRUCTION

    for text in (FINDINGS_ONLY_INSTRUCTION, SINGLE_SHOT_INSTRUCTION):
        assert "false positive" in text
        assert "retract" in text.lower()
