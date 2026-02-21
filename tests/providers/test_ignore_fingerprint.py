"""Tests for ignore list built from fingerprint in comment marker (Phase 2)."""

import hashlib

from code_review.diff.fingerprint import format_comment_body_with_marker, parse_marker_from_comment_body
from code_review.runner import _build_ignore_set


def test_ignore_set_includes_fingerprint_when_marker_present():
    body = format_comment_body_with_marker(
        "[Suggestion] Use a constant.", "fp789xyz", "0.1.0", run_id="r1"
    )
    comments = [{"path": "src/main.py", "body": body}]
    s = _build_ignore_set(comments)
    assert ("src/main.py", "fp789xyz") in s
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    assert ("src/main.py", body_hash) in s


def test_parse_marker_extracts_fingerprint_and_run():
    body = "<!-- code-review-agent:fingerprint=abc;version=0.1.0;run=run-id -->\n\nText"
    parsed = parse_marker_from_comment_body(body)
    assert parsed["fingerprint"] == "abc"
    assert parsed["run"] == "run-id"
