"""Anchor-based finding line relocation."""

from __future__ import annotations

import logging

from code_review.diff.line_index import build_per_file_line_index
from code_review.diff.utils import normalize_path
from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)

_ANCHOR_RELOCATION_WINDOW = 20


def _find_closest_anchor_line(
    lines_map: dict[int, str],
    anchor_text: str,
    reported_line: int,
    window: int,
) -> int | None:
    """Return the closest line number whose content contains *anchor_text* (case-insensitive).

    Only considers lines within *window* of *reported_line*.  Returns ``None``
    when no match is found or the best match is the reported line itself.
    """
    anchor_lower = anchor_text.lower()
    best_line: int | None = None
    best_distance = window + 1
    for ln, content in lines_map.items():
        if anchor_lower not in content.lower():
            continue
        distance = abs(ln - reported_line)
        if distance <= window and distance < best_distance:
            best_line = ln
            best_distance = distance
    return best_line


def _maybe_relocate_finding(
    f: FindingV1,
    file_lines: dict[str, dict[int, str]],
    window: int,
) -> FindingV1:
    """Return *f* relocated to the correct line if anchor text doesn't match, else unchanged."""
    anchor_text = (f.anchor or f.fingerprint_hint or "").strip()
    if not anchor_text:
        return f

    norm_path = normalize_path(f.path)
    lines_map = file_lines.get(norm_path)
    if not lines_map:
        return f

    current_content = lines_map.get(f.line, "")
    if anchor_text.lower() in current_content.lower():
        return f

    best_line = _find_closest_anchor_line(lines_map, anchor_text, f.line, window)
    if best_line is not None and best_line != f.line:
        logger.info(
            "Relocating finding %s:%d -> %d (anchor %r found at line %d)",
            f.path,
            f.line,
            best_line,
            anchor_text,
            best_line,
        )
        update: dict = {"line": best_line}
        if f.end_line is not None:
            delta = best_line - f.line
            new_end = f.end_line + delta
            update["end_line"] = max(new_end, best_line)
        return f.model_copy(update=update)
    return f


def relocate_findings_by_anchor(
    findings: list[FindingV1],
    diff_text: str,
    window: int = _ANCHOR_RELOCATION_WINDOW,
) -> list[FindingV1]:
    """Correct finding line numbers when the anchor text doesn't match the reported line.

    The LLM sometimes identifies the right code issue but reports a line number
    that is off by a few lines.  When a finding has a non-empty ``anchor`` or
    ``fingerprint_hint``, this function checks whether that text appears in the
    diff content at ``finding.line``.  If not, it searches nearby visible lines
    (within *window* lines above and below) in the same file and relocates the
    finding to the closest line whose content contains the anchor substring.

    Findings without an anchor/fingerprint_hint, or whose anchor already matches
    at the reported line, are returned unchanged.
    """
    if not diff_text or not findings:
        return findings

    file_lines = build_per_file_line_index(diff_text)

    result: list[FindingV1] = []
    for f in findings:
        result.append(_maybe_relocate_finding(f, file_lines, window))
    return result
