"""Diff line index builders."""

from __future__ import annotations

from code_review.diff.analyzer import DiffAnalyzer
from code_review.diff.parser import parse_unified_diff


def build_diff_line_index(diff_text: str) -> dict[tuple[str, int], str]:
    """Build a mapping of (normalized_path, new_line) -> stripped line content from the diff.

    Only includes lines visible in the new-file view (ADDED '+' and CONTEXT ' ').
    Used by _validate_suggested_patches to check whether a patch is anchored to the
    correct line.
    """
    index: dict[tuple[str, int], str] = {}
    for hunk in parse_unified_diff(diff_text):
        norm_path = DiffAnalyzer.normalize_path(hunk.path)
        for content, _old_ln, new_ln in hunk.lines:
            if new_ln is not None:
                index[(norm_path, new_ln)] = content.strip()
    return index


def build_per_file_line_index(
    diff_text: str,
) -> dict[str, dict[int, str]]:
    """Build {normalized_path: {new_line_no: stripped_content}} from a unified diff.

    Only includes lines visible in the new-file view (ADDED and CONTEXT).
    """
    file_lines: dict[str, dict[int, str]] = {}
    for hunk in parse_unified_diff(diff_text):
        norm_path = DiffAnalyzer.normalize_path(hunk.path)
        bucket = file_lines.setdefault(norm_path, {})
        for content, _old_ln, new_ln in hunk.lines:
            if new_ln is not None:
                bucket[new_ln] = content.strip()
    return file_lines
