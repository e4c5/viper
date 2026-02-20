"""Fingerprinting for deduplication and resolved-issue tracking."""

import hashlib
import re


def normalize_anchor(text: str) -> str:
    """Normalize line text for anchor matching (whitespace collapse)."""
    return re.sub(r"\s+", " ", text.strip())


def content_hash(content: str) -> str:
    """SHA256 hash of content for fingerprinting."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def build_fingerprint(
    path: str,
    content_hash_val: str,
    issue_code: str,
    anchor: str | None = None,
) -> str:
    """
    Build a stable fingerprint for a finding.
    Used for ignore list and auto-resolve.
    """
    parts = [path, content_hash_val, issue_code]
    if anchor is not None:
        parts.append(normalize_anchor(anchor))
    raw = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()
    return raw[:24]
