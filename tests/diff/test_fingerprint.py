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
    assert hashlib.sha256(b"hello").hexdigest().startswith(h)


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
    assert body.startswith("<!-- ")
    assert body.endswith("Hello.")
    body_with_run = format_comment_body_with_marker("Hi.", "fp", "1.0", run_id="run-xyz")
    assert "run=run-xyz" in body_with_run


def test_format_comment_body_with_marker_at_end():
    """When marker_at_end=True (e.g. Bitbucket), visible text comes first; marker at end."""
    body = format_comment_body_with_marker("Visible comment.", "fp99", "0.1.0", marker_at_end=True)
    assert body.startswith("Visible comment.")
    assert body.endswith("-->")  # marker HTML comment at end
    assert "<!-- code-review-agent:" in body
    assert "fingerprint=fp99" in body
    # Parser still finds the marker anywhere in body
    out = parse_marker_from_comment_body(body)
    assert out["fingerprint"] == "fp99"
    assert out["version"] == "0.1.0"


def test_parse_marker_from_comment_body():
    body = "<!-- code-review-agent:fingerprint=abc;version=0.1.0;run=key1 -->\n\n[High] Fix."
    out = parse_marker_from_comment_body(body)
    assert out["fingerprint"] == "abc"
    assert out["version"] == "0.1.0"
    assert out["run"] == "key1"


def test_format_and_parse_commonmark_linkref_marker():
    """Bitbucket DC/Server: unused link reference is not rendered; still round-trips."""
    body = format_comment_body_with_marker(
        "Visible **text**.",
        "fpz",
        "0.1.0",
        run_id="gitea/o/r/pr/1/head/sha/agent/0.1.0/config/abc",
        marker_at_end=True,
        use_commonmark_linkref=True,
    )
    assert body.startswith("Visible **text**.")
    assert "[__code_review_agent__]:" in body
    assert "<!--" not in body
    out = parse_marker_from_comment_body(body)
    assert out["fingerprint"] == "fpz"
    assert out["version"] == "0.1.0"
    assert out["run"] == "gitea/o/r/pr/1/head/sha/agent/0.1.0/config/abc"

    empty = parse_marker_from_comment_body("no marker")
    assert empty["run"] is None
