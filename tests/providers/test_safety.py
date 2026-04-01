"""Tests for repo content safety (truncation, delimiter)."""

from code_review.providers.safety import (
    MAX_REPO_FILE_BYTES,
    TRUNCATE_SUFFIX,
    truncate_repo_content,
)


def test_truncate_repo_content_under_limit():
    content = "line1\nline2"
    assert truncate_repo_content(content, max_bytes=100) == content


def test_truncate_repo_content_over_limit():
    content = "a" * (20 * 1024)
    result = truncate_repo_content(content, max_bytes=MAX_REPO_FILE_BYTES)
    assert result.endswith(TRUNCATE_SUFFIX)
    assert len(result.encode("utf-8")) <= MAX_REPO_FILE_BYTES


def test_truncate_repo_content_utf8_boundary():
    # Multi-byte chars (é = 2 bytes); errors="ignore" drops incomplete trailing bytes
    max_bytes = 100
    content = "\u00e9" * 6000  # 12000 bytes
    result = truncate_repo_content(content, max_bytes=max_bytes)
    assert result.endswith(TRUNCATE_SUFFIX)
    assert len(result.encode("utf-8")) <= max_bytes
