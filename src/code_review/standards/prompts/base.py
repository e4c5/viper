"""Base review criteria prompt fragments."""

from __future__ import annotations

from pathlib import Path


_PROMPTS_DIR = Path(__file__).parent


def _read_prompt_fragment(filename: str) -> str:
    """
    Read a prompt fragment from a sibling .md file.

    Returns an empty string if the file does not exist so callers can
    safely concatenate fragments.
    """
    path = _PROMPTS_DIR / filename
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except FileNotFoundError:
        return ""


BASE_REVIEW_PROMPT = _read_prompt_fragment("base.md")
