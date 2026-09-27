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
        from datetime import datetime
        from email.utils import parsedate_to_datetime

        retry_at = parsedate_to_datetime(text)
        return max(0.0, (retry_at - datetime.now(retry_at.tzinfo)).total_seconds())
    except Exception:
        return None
