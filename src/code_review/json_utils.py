"""Helpers for extracting JSON payloads from raw LLM text."""

from __future__ import annotations

from collections.abc import Iterator


def iter_json_candidates(
    text: str, *, repair_python_escaped_apostrophes: bool = False
) -> Iterator[str]:
    """Yield unique JSON payload candidates from raw text and fenced code blocks."""
    body = text.strip()
    candidates: list[str] = []

    fenced = _extract_first_jsonish_fence(body)
    if fenced:
        candidates.append(fenced)
    candidates.append(body)

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        yield candidate
        if repair_python_escaped_apostrophes and "\\'" in candidate:
            yield candidate.replace("\\'", "'")


def _extract_first_jsonish_fence(text: str) -> str | None:
    """Return the first unlabeled or json-labeled fenced block, if present."""
    start = text.find("```")
    while start != -1:
        cursor = start + 3
        while cursor < len(text) and text[cursor] in " \t":
            cursor += 1

        info_start = cursor
        while cursor < len(text) and text[cursor] not in " \t\r\n`":
            cursor += 1
        language = text[info_start:cursor].lower()

        while cursor < len(text) and text[cursor] in " \t\r\n":
            cursor += 1

        if language in ("", "json"):
            end = text.find("```", cursor)
            if end != -1:
                return text[cursor:end].strip()

        start = text.find("```", start + 3)

    return None
