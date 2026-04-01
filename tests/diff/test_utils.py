"""Tests for shared diff utility helpers."""

from code_review.diff.utils import normalize_path


def test_normalize_path_strips_provider_and_git_prefixes_by_default() -> None:
    assert normalize_path("dst:///a/src/foo.py") == "src/foo.py"
    assert normalize_path("b/src/foo.py") == "src/foo.py"


def test_normalize_path_can_preserve_git_prefixes() -> None:
    assert normalize_path("a/src/foo.py", strip_git_prefixes=False) == "a/src/foo.py"
    assert normalize_path("src:///foo.py", strip_git_prefixes=False) == "foo.py"
