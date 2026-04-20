"""Tests for language/context detection."""

from code_review.standards.detector import detect_from_paths_and_content


def test_detect_language_context_paths_only():
    ctx = detect_from_paths_and_content(["foo.py", "bar.py", "requirements.txt"], {})
    assert ctx.language == "python"
    assert ctx.confidence in ("high", "medium", "low")


def test_detect_language_context_with_sample():
    ctx = detect_from_paths_and_content(
        ["requirements.txt", "src/main.py"],
        {"requirements.txt": "fastapi>=0.100\ndjango>=4.0\n"},
    )
    assert ctx.language == "python"
    assert ctx.framework in ("fastapi", "django", None)


def test_detect_language_context_empty():
    ctx = detect_from_paths_and_content([], {})
    assert ctx.language == "unknown"
    assert ctx.framework is None
    assert ctx.confidence == "low"
