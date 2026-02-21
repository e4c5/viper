"""Tests for prompt content: severity labels and non-empty templates (Phase 4)."""

from code_review.standards.prompts import get_review_standards
from code_review.standards.prompts.base import BASE_REVIEW_PROMPT


def test_base_review_prompt_contains_severity_labels():
    """Base prompt must mention [Critical], [Suggestion], [Info] for comment format."""
    assert "[Critical]" in BASE_REVIEW_PROMPT
    assert "[Suggestion]" in BASE_REVIEW_PROMPT
    assert "[Info]" in BASE_REVIEW_PROMPT


def test_base_review_prompt_not_empty():
    """Base prompt is non-empty and has substantial content."""
    text = BASE_REVIEW_PROMPT.strip()
    assert len(text) > 100
    assert "Review" in text or "review" in text


def test_get_review_standards_returns_non_empty():
    """get_review_standards returns non-empty string for known and unknown language."""
    for lang in ("python", "javascript", "go", "unknown"):
        result = get_review_standards(lang, None)
        assert isinstance(result, str)
        assert len(result.strip()) > 0


def test_get_review_standards_includes_base_content():
    """Combined standards include base prompt content."""
    result = get_review_standards("python", None)
    assert "[Critical]" in result
    assert "[Suggestion]" in result
    assert "[Info]" in result


def test_get_review_standards_language_fragment():
    """Known language adds language-specific fragment."""
    result_python = get_review_standards("python", None)
    result_unknown = get_review_standards("unknown", None)
    assert "Python" in result_python or "PEP" in result_python
    # Unknown language still gets base only
    assert "[Critical]" in result_unknown


def test_get_review_standards_framework_appended():
    """Framework is appended when provided."""
    result = get_review_standards("python", "Django")
    assert "Framework" in result
    assert "Django" in result
