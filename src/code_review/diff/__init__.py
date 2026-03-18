"""Diff parsing and comment positioning."""

from code_review.diff.fingerprint import (
    build_fingerprint,
    content_hash,
    format_comment_body_with_marker,
    normalize_anchor,
    parse_marker_from_comment_body,
    surrounding_content_hash,
)
from code_review.diff.parser import DiffHunk, annotate_diff_with_line_numbers, iter_new_lines, parse_unified_diff
from code_review.diff.position import (
    CommentablePosition,
    get_commentable_positions,
    get_diff_hunk_for_line,
    position_for_line,
)

__all__ = [
    "annotate_diff_with_line_numbers",
    "build_fingerprint",
    "CommentablePosition",
    "content_hash",
    "DiffHunk",
    "format_comment_body_with_marker",
    "get_commentable_positions",
    "get_diff_hunk_for_line",
    "iter_new_lines",
    "normalize_anchor",
    "parse_marker_from_comment_body",
    "parse_unified_diff",
    "position_for_line",
    "surrounding_content_hash",
]
