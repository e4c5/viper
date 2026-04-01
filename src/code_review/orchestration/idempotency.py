"""Idempotency helpers for the orchestration layer."""

from __future__ import annotations

from code_review.diff.fingerprint import parse_marker_from_comment_body


def _idempotency_key_seen_in_comments(comments: list, key: str) -> bool:
    """Return True if any comment body contains run=<key> in a code-review-agent marker."""
    for c in comments:
        body = getattr(c, "body", None) or (c.get("body") if isinstance(c, dict) else "")
        if body:
            parsed = parse_marker_from_comment_body(body)
            if parsed.get("run") == key:
                return True
    return False
