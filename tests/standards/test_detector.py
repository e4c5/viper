"""Tests for language/framework detector."""

import pytest

from code_review.standards import (
    detect_from_paths,
    detect_from_paths_and_content,
    detect_from_paths_per_folder_root,
)
from code_review.standards.detector import (
    CONFIDENCE_THRESHOLD_HIGH,
    CONFIDENCE_THRESHOLD_MEDIUM,
    _confidence_from_score,
)


def test_confidence_thresholds():
    """Confidence literal from numeric score (0.0-1.0)."""
    assert _confidence_from_score(0.0) == "low"
    assert _confidence_from_score(0.4) == "low"
    assert _confidence_from_score(CONFIDENCE_THRESHOLD_MEDIUM) == "medium"
    assert _confidence_from_score(0.7) == "medium"
    assert _confidence_from_score(CONFIDENCE_THRESHOLD_HIGH) == "high"
    assert _confidence_from_score(1.0) == "high"


def test_detect_from_paths_empty():
    out = detect_from_paths([])
    assert out.language == "unknown"
    assert out.framework is None
    assert out.confidence == "low"
    assert out.confidence_score == pytest.approx(0.0)


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
    assert out.confidence_score >= CONFIDENCE_THRESHOLD_HIGH


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
    assert out.confidence_score == pytest.approx(0.0)


def test_detect_from_paths_confidence_score_in_range():
    """confidence_score is always 0.0-1.0."""
    for paths in [["a.py"], ["a.py", "b.py"], ["a.py", "b.py", "c.py"], ["a.ts", "b.ts"]]:
        out = detect_from_paths(paths)
        assert 0.0 <= out.confidence_score <= 1.0


def test_detect_from_paths_per_folder_root_monorepo():
    """Monorepo: per-folder-root detection when config files present."""
    paths = [
        "pkg-python/requirements.txt",
        "pkg-python/app/main.py",
        "pkg-go/go.mod",
        "pkg-go/cmd/server.go",
    ]
    result = detect_from_paths_per_folder_root(paths)
    assert "" not in result or result[""].language in ("unknown", "python", "go")
    # Should have at least one folder root (pkg-python or pkg-go)
    by_root = {k: v for k, v in result.items() if k}
    assert len(by_root) >= 1
    if "pkg-python" in by_root:
        assert by_root["pkg-python"].language == "python"
    if "pkg-go" in by_root:
        assert by_root["pkg-go"].language == "go"


def test_detect_from_paths_per_folder_root_single_root():
    """All paths at repo root -> single entry for ''."""
    paths = ["foo.py", "bar.py"]
    result = detect_from_paths_per_folder_root(paths)
    assert list(result.keys()) == [""]
    assert result[""].language == "python"


def test_detect_from_paths_per_folder_root_empty():
    """Empty paths -> empty dict."""
    assert detect_from_paths_per_folder_root([]) == {}


def test_detect_from_paths_per_folder_root_overlapping_roots():
    """Overlapping roots: longest-prefix root wins."""
    paths = [
        "services/api/pyproject.toml",
        "services/api/app/main.py",
        "services/api-admin/pyproject.toml",
        "services/api-admin/app/admin.py",
    ]
    result = detect_from_paths_per_folder_root(paths)
    # Both roots should be present with python language
    assert "services/api" in result
    assert "services/api-admin" in result
    assert result["services/api"].language == "python"
    assert result["services/api-admin"].language == "python"


def test_detect_from_paths_per_folder_root_orphan_files_grouped_under_empty_root():
    """Files not under any config root are grouped under ''."""
    paths = [
        "pkg-python/requirements.txt",
        "pkg-python/app/main.py",
        "scripts/one_off.py",
    ]
    result = detect_from_paths_per_folder_root(paths)
    assert "pkg-python" in result
    assert "" in result
    assert result["pkg-python"].language == "python"
    # Orphan script should still be classified (likely python) under repo root
    assert result[""].language in ("python", "unknown")
