"""Shared diff utility helpers."""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Return a coarse chars/4 token estimate used across diff workflows."""
    return max(0, len(text) // 4)


def normalize_path(path: str, *, strip_git_prefixes: bool = True) -> str:
    """Normalize file paths used across diff parsing and SCM anchor matching.

    Removes provider-style ``src://`` / ``dst://`` prefixes and leading slashes.
    When ``strip_git_prefixes`` is true, also removes a leading ``a/`` or ``b/``
    added by unified diff headers.
    """
    normalized = (path or "").strip()
    for prefix in ("dst://", "src://"):
        if normalized.lower().startswith(prefix):
            normalized = normalized[len(prefix) :].lstrip("/")
            break
    normalized = normalized.lstrip("/")
    if strip_git_prefixes:
        for prefix in ("a/", "b/"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
                break
        normalized = normalized.lstrip("/")
    return normalized or path or ""
