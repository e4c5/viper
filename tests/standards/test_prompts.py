"""Tests for prompt content: severity labels and non-empty templates (Phase 4)."""

import pytest

from code_review.standards.prompts import get_review_standards
from code_review.standards.prompts.base import BASE_REVIEW_PROMPT, _read_prompt_fragment


def test_base_review_prompt_contains_severity_labels():
    """Base prompt must mention [High], [Medium], [Low] for comment format."""
    assert "[High]" in BASE_REVIEW_PROMPT
    assert "[Medium]" in BASE_REVIEW_PROMPT
    assert "[Low]" in BASE_REVIEW_PROMPT


def test_base_review_prompt_discourages_speculative_log_level_downgrades():
    """Base prompt discourages speculative log-level downgrades without evidence."""
    text = BASE_REVIEW_PROMPT
    assert "Avoid speculative logging-level suggestions" in text
    assert "unless there is clear evidence" in text


def test_base_review_prompt_not_empty():
    """Base prompt is non-empty and has substantial content."""
    text = BASE_REVIEW_PROMPT.strip()
    assert len(text) > 100
    assert "Review" in text or "review" in text


def test_base_review_prompt_includes_test_code_criteria():
    """Test-only review criteria (issue #49): scoped checks for assertions and complexity."""
    assert "Test code only" in BASE_REVIEW_PROMPT
    assert "vacuous" in BASE_REVIEW_PROMPT.lower()
    assert "mega-tests" in BASE_REVIEW_PROMPT.lower()
    assert "false positives" in BASE_REVIEW_PROMPT.lower()


def test_get_review_standards_returns_non_empty():
    """get_review_standards returns non-empty string for known and unknown language."""
    for lang in ("python", "javascript", "go", "unknown"):
        result = get_review_standards(lang, None)
        assert isinstance(result, str)
        assert len(result.strip()) > 0


def test_get_review_standards_includes_base_content():
    """Combined standards include base prompt content."""
    result = get_review_standards("python", None)
    assert "[High]" in result
    assert "[Medium]" in result
    assert "[Low]" in result


def test_get_review_standards_language_fragment():
    """Known language adds language-specific fragment."""
    result_python = get_review_standards("python", None)
    result_unknown = get_review_standards("unknown", None)
    assert "Python" in result_python or "PEP" in result_python
    # Unknown language still gets base only
    assert "[High]" in result_unknown


def test_get_review_standards_framework_appended():
    """Framework is appended when provided."""
    result = get_review_standards("python", "Django")
    assert "Framework" in result
    assert "Django" in result


def test_required_prompt_fragment_raises_when_missing():
    """Required prompt fragments should fail fast instead of silently returning empty text."""
    with pytest.raises(RuntimeError, match="Missing prompt fragment"):
        _read_prompt_fragment("missing-required-fragment.md", required=True)


# --- Tests for improved prompt content ---


def test_python_fragment_contains_framework_guidance():
    """Python fragment includes framework-specific guidance for Django and FastAPI."""
    result = get_review_standards("python", None)
    assert "Django" in result
    assert "FastAPI" in result or "Starlette" in result


def test_javascript_fragment_contains_framework_guidance():
    """JavaScript fragment includes guidance for React/Vue and Node.js."""
    result = get_review_standards("javascript", None)
    assert "React" in result or "Vue" in result
    assert "Node" in result or "Node.js" in result


def test_typescript_fragment_contains_framework_guidance():
    """TypeScript fragment includes guidance for framework-specific patterns."""
    result = get_review_standards("typescript", None)
    assert "React" in result or "Angular" in result or "Next.js" in result
    assert "Node" in result or "Express" in result


def test_go_fragment_contains_http_and_db_guidance():
    """Go fragment includes guidance for HTTP and database patterns."""
    result = get_review_standards("go", None)
    assert "HTTP" in result or "handler" in result or "http" in result
    assert "SQL" in result or "database" in result or "transaction" in result


def test_java_fragment_contains_spring_guidance():
    """Java fragment includes Spring/Jakarta-specific guidance."""
    result = get_review_standards("java", None)
    assert "Spring" in result or "Jakarta" in result
    assert "transaction" in result or "Transactional" in result
