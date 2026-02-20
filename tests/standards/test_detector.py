"""Tests for language/framework detector."""

import pytest

from code_review.standards import detect_from_paths, detect_from_paths_and_content


def test_detect_from_paths_empty():
    out = detect_from_paths([])
    assert out.language == "unknown"
    assert out.framework is None
    assert out.confidence == "low"


def test_detect_from_paths_extension_python():
    out = detect_from_paths(["src/main.py", "tests/test_foo.py"])
    assert out.language == "python"
    assert out.framework is None
    assert out.confidence in ("medium", "high")


def test_detect_from_paths_extension_typescript():
    out = detect_from_paths(["app.ts", "lib/foo.tsx"])
    assert out.language == "typescript"
    assert out.framework is None


def test_detect_from_paths_path_signals_nextjs():
    out = detect_from_paths(["next.config.js", "pages/index.js"])
    assert out.language == "javascript"
    assert out.framework == "nextjs"


def test_detect_from_paths_path_signals_requirements():
    out = detect_from_paths(["requirements.txt", "app/foo.py"])
    assert out.language == "python"
    assert out.framework is None


def test_detect_from_paths_confidence_high():
    paths = ["a.py", "b.py", "c.py", "d.py"]
    out = detect_from_paths(paths)
    assert out.language == "python"
    assert out.confidence == "high"


def test_detect_from_paths_and_content_python_framework():
    paths = ["requirements.txt", "app/main.py"]
    content_by_path = {"requirements.txt": "django==4.0\nflask"}
    out = detect_from_paths_and_content(paths, content_by_path)
    assert out.language == "python"
    assert out.framework in ("django", "flask")
    assert out.confidence in ("medium", "high")


def test_detect_from_paths_and_content_pyproject_fastapi():
    paths = ["pyproject.toml", "src/main.py"]
    content_by_path = {"pyproject.toml": '[project]\ndependencies = ["fastapi"]'}
    out = detect_from_paths_and_content(paths, content_by_path)
    assert out.language == "python"
    assert out.framework == "fastapi"


def test_detect_from_paths_and_content_no_false_positive_django():
    """my-django-app should not match as framework django (word boundary)."""
    paths = ["requirements.txt"]
    content_by_path = {"requirements.txt": "my-django-app==1.0"}
    out = detect_from_paths_and_content(paths, content_by_path)
    assert out.language == "python"
    assert out.framework is None


def test_detect_from_paths_unknown_extension():
    out = detect_from_paths(["README.md", "LICENSE"])
    assert out.language == "unknown"
    assert out.framework is None
    assert out.confidence == "low"
