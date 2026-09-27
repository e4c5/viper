"""Retry-After header parsing shared by the SCM HTTP retry layer and the
orchestration-side LLM error helpers (kept in providers/ to avoid an
orchestration -> providers layering inversion)."""

from __future__ import annotations


def parse_retry_after(value: object) -> float | None:
    """Parse a Retry-After header value (seconds or HTTP-date) into seconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        retry_at = parsedate_to_datetime(text)
        if retry_at.tzinfo is None:
            # Naive HTTP-dates are always GMT per RFC 7231.
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None
