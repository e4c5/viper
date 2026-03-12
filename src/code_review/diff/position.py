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
        for _content, _old_ln, new_ln in hunk.lines:
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


def get_diff_hunk_for_line(diff_text: str, path: str, line: int) -> str | None:
    """
    Return the raw diff hunk (e.g. for GitHub/Gitea diff_hunk) that contains the given line
    in the new file. Path comparison is normalized (no leading slash).
    Returns None if no hunk contains (path, line).
    """
    path_norm = path.lstrip("/")
    for hunk in parse_unified_diff(diff_text):
        if hunk.path.lstrip("/") != path_norm:
            continue
        # Check if line is in this hunk's new-file range
        new_end = hunk.new_start + hunk.new_count - 1
        if not (hunk.new_start <= line <= new_end):
            continue
        # Rebuild hunk lines: @@ header then content lines with prefix
        lines: list[str] = []
        lines.append(
            f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@"
        )
        for content, old_ln, new_ln in hunk.lines:
            if old_ln is not None and new_ln is not None:
                prefix = " "
            elif new_ln is not None:
                prefix = "+"
            elif old_ln is not None:
                prefix = "-"
            else:
                prefix = "\\"
            lines.append(prefix + content)
        return "\n".join(lines)
    return None
