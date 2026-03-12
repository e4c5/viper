"""Per-language review prompt fragments (runner-side)."""

from __future__ import annotations

from pathlib import Path

from code_review.standards.prompts.base import BASE_REVIEW_PROMPT, _read_prompt_fragment


_PROMPTS_DIR = Path(__file__).parent

_LANGUAGE_FILES: dict[str, str] = {
    "python": "python.md",
    "javascript": "javascript.md",
    "typescript": "typescript.md",
    "go": "go.md",
    "java": "java.md",
    "c": "c.md",
    "cpp": "cpp.md",
}


def _load_language_fragment(language_key: str) -> str:
    """
    Load the language-specific review fragment from a .md file if present.
    """
    filename = _LANGUAGE_FILES.get(language_key)
    if not filename:
        return ""
    return _read_prompt_fragment(filename)


def get_review_standards(language: str, framework: str | None) -> str:
    """
    Return combined prompt fragment for the given language and framework.
    Runner-side; not an agent tool.
    """
    parts: list[str] = [BASE_REVIEW_PROMPT]
    lang_key = (language or "").lower()
    fragment = _load_language_fragment(lang_key)
    if fragment:
        parts.append(fragment)
    if framework:
        parts.append(f"\n### Framework: {framework}\n")
    return "\n".join(parts)
