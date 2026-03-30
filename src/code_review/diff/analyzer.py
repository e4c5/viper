"""Diff analysis utilities: token estimation and path normalization."""

from __future__ import annotations


class DiffAnalyzer:
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough token estimate (chars / 4) for diff and context budget checks."""
        return max(0, len(text) // 4)

    @staticmethod
    def normalize_path(file_path: str) -> str:
        """Normalize path like Bitbucket provider for diff line matching.

        Strips ``dst://``, ``src://``, ``a/``, and ``b/`` prefixes.
        """
        p = (file_path or "").strip()
        for prefix in ("dst://", "src://"):
            if p.lower().startswith(prefix):
                p = p[len(prefix) :].lstrip("/")
                break
        p = p.lstrip("/")
        for prefix in ("a/", "b/"):
            if p.startswith(prefix):
                p = p[len(prefix) :]
                break
        return p.lstrip("/") or file_path or ""


def estimate_tokens(text: str) -> int:
    return DiffAnalyzer.estimate_tokens(text)


def normalize_path_for_anchor(file_path: str) -> str:
    return DiffAnalyzer.normalize_path(file_path)
