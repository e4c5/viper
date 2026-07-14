"""Helpers for extracting JSON payloads from raw LLM text."""

from __future__ import annotations

from collections.abc import Iterator


def iter_json_candidates(
    text: str,
    *,
    repair_python_escaped_apostrophes: bool = False,
    include_embedded_objects: bool = False,
) -> Iterator[str]:
    """Yield unique JSON payload candidates from raw text and fenced code blocks.

    include_embedded_objects: also fall back to scanning for balanced {...}
    objects anywhere in the text, preferring the last one found. Reasoning
    models (e.g. DeepSeek) often prefix their final JSON answer with unfenced
    chain-of-thought prose instead of returning JSON-only or a fenced block;
    models instructed to end with the JSON answer put it after their
    reasoning. Off by default: some callers deliberately want to reject
    messy prefixed/suffixed text as a signal the model didn't follow
    instructions, rather than salvage an answer from it.
    """
    body = text.strip()
    candidates: list[str] = []

    fenced = _extract_first_jsonish_fence(body)
    if fenced:
        candidates.append(fenced)
    candidates.append(body)
    if include_embedded_objects:
        candidates.extend(reversed(_extract_balanced_json_objects(body)))

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        yield candidate
        if repair_python_escaped_apostrophes and "\\'" in candidate:
            yield candidate.replace("\\'", "'")


def _extract_balanced_json_objects(text: str) -> list[str]:
    """Return all top-level balanced {...} substrings found anywhere in text.

    Brace counting ignores braces inside double-quoted strings so that prose
    or code snippets containing literal `{`/`}` characters don't throw off
    matching of the actual JSON object(s).
    """
    objects: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(text[start : i + 1])
                    start = None
    return objects


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

        end = text.find("```", cursor)
        if end == -1:
            return None

        if language in ("", "json"):
            return text[cursor:end].strip()

        start = text.find("```", end + 3)

    return None
