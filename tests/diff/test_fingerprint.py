"""Tests for diff fingerprint (marker, content hash, build_fingerprint)."""

import hashlib

from code_review.diff.fingerprint import (
    build_fingerprint,
    content_hash,
    format_comment_body_with_marker,
    normalize_anchor,
    parse_marker_from_comment_body,
    surrounding_content_hash,
)


def test_normalize_anchor():
    assert normalize_anchor("  foo  bar  ") == "foo bar"
    assert normalize_anchor("a\t\nb") == "a b"


def test_content_hash():
    h = content_hash("hello")
    assert len(h) == 16
    assert h == hashlib.sha256(b"hello").hexdigest()[:16]


def test_surrounding_content_hash():
    lines = ["a", "b", "c", "d", "e"]
    h = surrounding_content_hash(lines, 3, window=1)
    assert h == content_hash("b\nc\nd")
    assert surrounding_content_hash([], 1, 2) == content_hash("")
    assert surrounding_content_hash(lines, 1, 2) == content_hash("a\nb\nc")


def test_surrounding_content_hash_line_beyond_file_length():
    """When line_1based > len(file_lines), returns content_hash('') for stability."""
    lines = ["a", "b"]
    assert surrounding_content_hash(lines, 10, window=2) == content_hash("")
    assert surrounding_content_hash(lines, 3, window=0) == content_hash("")


def test_build_fingerprint():
    fp = build_fingerprint("p.py", "chash", "unused-var", anchor="x = 1")
    assert len(fp) == 24
    fp2 = build_fingerprint("p.py", "chash", "unused-var", anchor="x = 1")
    assert fp == fp2
    fp3 = build_fingerprint("p.py", "chash", "other", anchor="x = 1")
    assert fp != fp3


def test_format_comment_body_with_marker():
    body = format_comment_body_with_marker("Hello.", "fp123", "0.1.0")
    assert "<!-- code-review-agent:" in body
    assert "fingerprint=fp123" in body
    assert "version=0.1.0" in body
    assert "Hello." in body
    body_with_run = format_comment_body_with_marker(
        "Hi.", "fp", "1.0", run_id="run-xyz"
    )
    assert "run=run-xyz" in body_with_run


def test_parse_marker_from_comment_body():
    body = '<!-- code-review-agent:fingerprint=abc;version=0.1.0;run=key1 -->\n\n[Critical] Fix.'
    out = parse_marker_from_comment_body(body)
    assert out["fingerprint"] == "abc"
    assert out["version"] == "0.1.0"
    assert out["run"] == "key1"
    out2 = parse_marker_from_comment_body("No marker here.")
    assert out2["fingerprint"] is None
    assert out2["version"] is None
    assert out2["run"] is None
