"""Shared diff utility helpers."""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Return a coarse chars/4 token estimate used across diff workflows."""
    return max(0, len(text) // 4)
