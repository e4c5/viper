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
    matching of the actual JSON object(s). Each `{` is tried as a fresh
    candidate start: an earlier unmatched/malformed `{` (e.g. from stray
    prose) doesn't poison the scan for a later, well-formed object, since we
    restart the attempt at the next `{` instead of carrying its depth
    forward.
    """
    objects: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escape = False
    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "{":
            end = _find_balanced_end(text, i)
            if end is not None:
                objects.append(text[i : end + 1])
                i = end + 1
                continue
        i += 1
    return objects


def _find_balanced_end(text: str, start: int) -> int | None:
    """Return the index of the closing brace matching text[start] == "{".

    Returns None if the brace opened at `start` is never closed (accounting
    for nested braces and skipping over braces inside quoted strings).
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
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
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


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
