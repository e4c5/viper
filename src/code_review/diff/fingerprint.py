"""Fingerprinting for deduplication and resolved-issue tracking."""

import hashlib
import re

# Hidden marker in comment body for fingerprint and idempotency (Phase 2)
COMMENT_MARKER_PREFIX = "<!-- code-review-agent:"
COMMENT_MARKER_SUFFIX = " -->"
_MARKER_RE = re.compile(
    re.escape(COMMENT_MARKER_PREFIX) + r"(.+?)" + re.escape(COMMENT_MARKER_SUFFIX)
)
_KEY_RE = re.compile(r"(fingerprint|version|run)=([^;]+)")


def normalize_anchor(text: str) -> str:
    """Normalize line text for anchor matching (whitespace collapse)."""
    return re.sub(r"\s+", " ", text.strip())


def content_hash(content: str) -> str:
    """SHA256 hash of content for fingerprinting."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def surrounding_content_hash(
    file_lines: list[str], line_1based: int, window: int = 2
) -> str:
    """
    Hash a window of lines around the given 1-based line.
    Used for (path, content_hash_of_surrounding_lines, issue_code) fingerprint.
    """
    if not file_lines or line_1based < 1:
        return content_hash("")
    # Line beyond file length: intentionally same as empty context for stability.
    if line_1based > len(file_lines):
        return content_hash("")
    start_idx = max(0, (line_1based - 1) - window)
    end_idx = min(len(file_lines), (line_1based - 1) + window + 1)
    span = "\n".join(file_lines[start_idx:end_idx])
    return content_hash(span)


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
    raw = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return raw[:24]


def format_comment_body_with_marker(
    body: str,
    fingerprint: str,
    version: str,
    run_id: str | None = None,
) -> str:
    """Prepend hidden marker to comment body for dedupe and idempotency."""
    parts = [f"fingerprint={fingerprint}", f"version={version}"]
    if run_id is not None:
        parts.append(f"run={run_id}")
    marker = COMMENT_MARKER_PREFIX + ";".join(parts) + COMMENT_MARKER_SUFFIX
    return marker + "\n\n" + body


def parse_marker_from_comment_body(body: str) -> dict[str, str | None]:
    """
    Parse code-review-agent marker from comment body.
    Returns dict with keys fingerprint, version, run (values None if absent).
    """
    out: dict[str, str | None] = {
        "fingerprint": None,
        "version": None,
        "run": None,
    }
    m = _MARKER_RE.search(body)
    if not m:
        return out
    inner = m.group(1)
    for key_m in _KEY_RE.finditer(inner):
        key, val = key_m.group(1), key_m.group(2).strip()
        if key in out:
            out[key] = val
    return out
