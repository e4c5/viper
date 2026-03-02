"""Fingerprinting for deduplication and resolved-issue tracking."""

import hashlib
import hmac
import os
import re

# Hidden marker in comment body for fingerprint and idempotency (Phase 2)
COMMENT_MARKER_PREFIX = "<!-- code-review-agent:"
COMMENT_MARKER_SUFFIX = " -->"
_MARKER_RE = re.compile(
    re.escape(COMMENT_MARKER_PREFIX) + r"(.+?)" + re.escape(COMMENT_MARKER_SUFFIX)
)
_KEY_RE = re.compile(r"(fingerprint|version|run)=([^;]+)")

_SIGNING_KEY_ENV = "CODE_REVIEW_SIGNING_KEY"


def _get_signing_key() -> bytes:
    """
    Return HMAC signing key for markers.

    Only CODE_REVIEW_SIGNING_KEY enables signing/verification. This keeps backward
    compatibility with existing unsigned markers and tests even when SCM_TOKEN is set.
    """
    key = os.environ.get(_SIGNING_KEY_ENV) or ""
    return key.encode("utf-8")


def _sign_marker(payload: str) -> str:
    """Return hex HMAC of marker payload."""
    key = _get_signing_key()
    if not key:
        return ""
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


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
    payload = ";".join(parts)
    sig = _sign_marker(payload)
    if sig:
        payload = payload + f";sig={sig}"
    marker = COMMENT_MARKER_PREFIX + payload + COMMENT_MARKER_SUFFIX
    return marker + "\n\n" + body


def parse_marker_from_comment_body(body: str) -> dict[str, str | None]:
    """
    Parse code-review-agent marker from comment body.
    Returns dict with keys fingerprint, version, run (values None if absent).
    Ignores markers with invalid HMAC signatures when a signing key is configured.
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
    # Split payload and optional sig
    segments = [seg for seg in inner.split(";") if seg]
    fields: dict[str, str] = {}
    sig_val: str | None = None
    for seg in segments:
        if "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k == "sig":
            sig_val = v
        elif k in ("fingerprint", "version", "run"):
            fields[k] = v
    key = _get_signing_key()
    if key:
        # Rebuild payload in canonical order for verification
        payload_parts = []
        if "fingerprint" in fields:
            payload_parts.append(f"fingerprint={fields['fingerprint']}")
        if "version" in fields:
            payload_parts.append(f"version={fields['version']}")
        if "run" in fields:
            payload_parts.append(f"run={fields['run']}")
        payload = ";".join(payload_parts)
        expected = _sign_marker(payload)
        if not sig_val or not expected or not hmac.compare_digest(sig_val, expected):
            return out
    for field in out:
        if field in fields:
            out[field] = fields[field]
    return out
