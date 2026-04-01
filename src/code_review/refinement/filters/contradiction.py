"""Contradiction-based finding filter."""

from __future__ import annotations

import logging
import re

from code_review.diff.line_index import build_diff_line_index, build_per_file_line_index
from code_review.diff.utils import normalize_path
from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)

_SYNTAX_OR_MISSING_TOKEN_MESSAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bmissing\s+(?:a\s+)?(?:comma|semicolon|colon|parenthesis|paren|bracket|brace|quote)\b",
        re.I,
    ),
    re.compile(r"\bsyntax\s+error\b", re.I),
    re.compile(r"\b(?:invalid|malformed)\s+(?:\w+\s+)?code\b", re.I),
    re.compile(r"\b(?:invalid|malformed)\s+(?:annotation|statement|expression)\b", re.I),
    re.compile(r"\b(?:won['\u2019]?t|will\s+not)\s+compile\b", re.I),
    re.compile(r"\bcompiler?\s+error\b", re.I),
)

_QUOTE_DELIMITERS = frozenset({"`", "'", '"'})
_MISSING_COMMA_BEFORE_FRAGMENT_PATTERN = re.compile(
    r"\bmissing\s+(?:a\s+)?comma\s+before\s+(?:`([^`]+)`|\"([^\"]+)\"|'([^']+)')",
    re.I,
)


def _patch_tokens(text: str) -> set[str]:
    """Return a set of non-trivial tokens (len >= 3) from a code string, lower-cased.

    Used for a rough content-similarity check between a suggested_patch and the
    actual diff line it is anchored to.
    """
    return {tok.lower() for tok in re.split(r"\W+", text) if len(tok) >= 3}


def _normalize_code_for_comparison(text: str) -> str:
    """Remove whitespace outside quoted literals so formatting-only changes compare equal."""
    if not text:
        return ""

    out: list[str] = []
    quote_char: str | None = None
    escaped = False

    for ch in text:
        if quote_char is not None:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                quote_char = None
            continue

        if ch in {'"', "'", "`"}:
            quote_char = ch
            out.append(ch)
            continue

        if ch.isspace():
            continue

        out.append(ch)

    return "".join(out)


def _contains_word(text: str, word: str) -> bool:
    """Return True when word appears delimited by non-word characters."""
    start = 0
    while True:
        index = text.find(word, start)
        if index == -1:
            return False
        before_ok = index == 0 or not (text[index - 1].isalnum() or text[index - 1] == "_")
        after_index = index + len(word)
        after_ok = after_index == len(text) or not (
            text[after_index].isalnum() or text[after_index] == "_"
        )
        if before_ok and after_ok:
            return True
        start = index + len(word)


def _message_mentions_missing_quoted_fragment_before_or_after(message: str) -> bool:
    """Detect messages like 'missing `x` before ...' without broad backtracking regexes."""
    lowered = message.lower()
    search_from = 0
    while True:
        missing_index = lowered.find("missing", search_from)
        if missing_index == -1:
            return False

        fragment_start = missing_index + len("missing")
        while fragment_start < len(message) and message[fragment_start].isspace():
            fragment_start += 1

        if fragment_start >= len(message) or message[fragment_start] not in _QUOTE_DELIMITERS:
            search_from = missing_index + len("missing")
            continue

        delimiter = message[fragment_start]
        fragment_end = message.find(delimiter, fragment_start + 1)
        if fragment_end == -1:
            return False

        trailing_text = lowered[fragment_end + 1 :]
        if _contains_word(trailing_text, "before") or _contains_word(trailing_text, "after"):
            return True
        search_from = fragment_end + 1


def _message_describes_syntax_or_missing_token_issue(message: str) -> bool:
    """True when the finding text claims a token/syntax defect rather than a semantic issue."""
    if not message or not str(message).strip():
        return False
    return any(p.search(message) for p in _SYNTAX_OR_MISSING_TOKEN_MESSAGE_PATTERNS) or (
        _message_mentions_missing_quoted_fragment_before_or_after(message)
    )


def _extract_missing_comma_fragment(message: str) -> str | None:
    """Extract the cited fragment from messages like 'missing comma before `nullable = false`'."""
    if not message:
        return None
    match = _MISSING_COMMA_BEFORE_FRAGMENT_PATTERN.search(message)
    if not match:
        return None
    for group in match.groups():
        if group and group.strip():
            return group.strip()
    return None


def _window_text(lines_map: dict[int, str], line: int, radius: int = 2) -> str:
    """Join nearby visible diff lines into a small searchable context window."""
    window_lines = [
        content for ln, content in sorted(lines_map.items()) if line - radius <= ln <= line + radius
    ]
    return "\n".join(window_lines)


def _non_empty_patch_lines(suggested_patch: str) -> list[str]:
    """Return stripped non-empty lines from a suggested patch block."""
    return [ln.strip() for ln in suggested_patch.splitlines() if ln.strip()]


def _drop_or_strip_identical_patch_finding(
    finding: FindingV1,
    *,
    actual_content: str,
    message: str,
) -> FindingV1 | None:
    """Drop contradicted syntax findings or strip redundant suggestions."""
    if not finding.suggested_patch:
        return finding

    non_empty_lines = _non_empty_patch_lines(finding.suggested_patch)
    if len(non_empty_lines) != 1:
        return finding

    matches_current_line = _normalize_code_for_comparison(
        non_empty_lines[0]
    ) == _normalize_code_for_comparison(actual_content)
    if not matches_current_line:
        return finding

    if _message_describes_syntax_or_missing_token_issue(message):
        logger.info(
            "Dropping contradicted syntax/token finding %s:%d: "
            "suggested patch is identical to current diff line",
            finding.path,
            finding.line,
        )
        return None

    return finding.model_copy(update={"suggested_patch": None})


def _contradicted_missing_comma_fragment(message: str, window_text: str) -> str | None:
    """Return the contradicted fragment when nearby diff already contains `,<fragment>`."""
    fragment = _extract_missing_comma_fragment(message)
    if not fragment:
        return None

    fragment_patterns = (
        f", {fragment}",
        f",{fragment}",
    )
    if any(pattern in window_text for pattern in fragment_patterns):
        return fragment
    return None


def filter_obviously_contradicted_findings(
    findings: list[FindingV1],
    diff_text: str,
) -> list[FindingV1]:
    """Drop findings whose own message is directly contradicted by visible diff code.

    This is intentionally conservative and only handles a few high-signal cases:
    - a syntax/token complaint whose suggested patch is identical to the current line
    - a message claiming a missing comma before a quoted fragment when the nearby diff
      already contains `,<fragment>`
    """
    if not diff_text or not findings:
        return findings

    line_index = build_diff_line_index(diff_text)
    file_lines = build_per_file_line_index(diff_text)
    kept: list[FindingV1] = []

    for f in findings:
        norm_path = normalize_path(f.path)
        actual_content = line_index.get((norm_path, f.line))
        lines_map = file_lines.get(norm_path, {})
        if actual_content is None:
            kept.append(f)
            continue

        message = f.message or ""
        window = _window_text(lines_map, f.line)
        f = _drop_or_strip_identical_patch_finding(
            f,
            actual_content=actual_content,
            message=message,
        )
        if f is None:
            continue

        fragment = _contradicted_missing_comma_fragment(message, window)
        if fragment is not None:
            logger.info(
                "Dropping contradicted missing-comma finding %s:%d: "
                "nearby diff already contains comma before %r",
                f.path,
                f.line,
                fragment,
            )
            continue

        kept.append(f)

    return kept
