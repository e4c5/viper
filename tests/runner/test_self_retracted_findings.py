"""Tests for dropping findings whose messages retract or negate the issue."""

from code_review.refinement.filters.self_retraction import (
    _finding_message_looks_self_retracted,
)
from code_review.refinement.filters.self_retraction import (
    filter_self_retracted_findings as _filter_self_retracted_finding_messages,
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


def test_self_retracted_false_positive_requires_self_reference():
    assert _finding_message_looks_self_retracted(
        "Possible NPE — this is a false positive, guarded above."
    ) is True
    assert _finding_message_looks_self_retracted("That was a false positive.") is True


def test_legitimate_retract_in_domain_language_not_dropped():
    msg = "Ensure the latch retracts before the door closes."
    assert _finding_message_looks_self_retracted(msg) is False


def test_will_retract_requires_first_person():
    assert _finding_message_looks_self_retracted("The latch will retract when released.") is False


def test_walk_back_phrases_match():
    assert _finding_message_looks_self_retracted("On second thought, keep the guard.") is True
    assert _finding_message_looks_self_retracted("Actually, this is fine as written.") is True
    assert _finding_message_looks_self_retracted("However, this is correct.") is True
    assert _finding_message_looks_self_retracted("I was wrong about the race.") is True


def test_false_positive_in_analysis_not_treated_as_retraction():
    assert (
        _finding_message_looks_self_retracted(
            "Tighten the guard: this check causes false positives when X is null."
        )
        is False
    )
    assert (
        _finding_message_looks_self_retracted(
            "Document why the heuristic has a high false positive rate on this path."
        )
        is False
    )


def test_filter_drops_retracted_keeps_normal():
    good = _f("Constructor should validate errors list is non-null.")
    bad = _f("Wait, actually fine. I will retract this finding.")
    out = _filter_self_retracted_finding_messages([good, bad])
    assert len(out) == 1
    assert out[0].message == good.message


def test_agent_instructions_discourage_self_retracting_messages():
    from code_review.agent.agent import (
        EMBEDDED_DIFF_REVIEW_INSTRUCTION,
        TOOL_ENABLED_REVIEW_INSTRUCTION,
    )

    for text in (TOOL_ENABLED_REVIEW_INSTRUCTION, EMBEDDED_DIFF_REVIEW_INSTRUCTION):
        lowered = text.lower()
        assert "false positive" in lowered
        assert "retract" in lowered
