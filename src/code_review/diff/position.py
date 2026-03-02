"""Commentable position mapping for inline review comments."""

from dataclasses import dataclass
from typing import Any

from code_review.diff.parser import parse_unified_diff


@dataclass
class CommentablePosition:
    """A position in the diff where an inline comment can be placed."""

    path: str
    line_in_new_file: int
    hunk_index: int
    api_coords: dict[str, Any]  # Provider-specific (e.g. line, position, diff_hunk)


def get_commentable_positions(diff_text: str) -> list[CommentablePosition]:
    """
    Build list of commentable positions from unified diff.
    Maps (path, line_in_new_file) to hunk index and API-specific coordinates.
    """
    hunks = parse_unified_diff(diff_text)
    positions: list[CommentablePosition] = []
    for hunk_idx, hunk in enumerate(hunks):
        for content, _old_ln, new_ln in hunk.lines:
            if new_ln is not None:
                positions.append(
                    CommentablePosition(
                        path=hunk.path,
                        line_in_new_file=new_ln,
                        hunk_index=hunk_idx,
                        api_coords={"line": new_ln, "path": hunk.path},
                    )
                )
    return positions


def position_for_line(diff_text: str, path: str, line: int) -> CommentablePosition | None:
    """
    Return the CommentablePosition for (path, line), or None if not commentable.
    """
    for pos in get_commentable_positions(diff_text):
        if pos.path == path and pos.line_in_new_file == line:
            return pos
    return None
