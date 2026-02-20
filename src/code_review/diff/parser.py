"""Parse unified diff format for line positions."""

import re
from dataclasses import dataclass


@dataclass
class DiffHunk:
    """A hunk of changed lines in a file."""

    path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, int | None, int | None]]  # (content, old_line, new_line)


def parse_unified_diff(diff_text: str) -> list[DiffHunk]:
    """
    Parse unified diff text into a list of DiffHunk.
    Each hunk contains lines with (content, old_line, new_line).
    old_line/new_line are None for context lines in add/remove-only hunks.
    """
    hunks: list[DiffHunk] = []
    current_path = ""
    current_old_start = 0
    current_old_count = 0
    current_new_start = 0
    current_new_count = 0
    current_lines: list[tuple[str, int | None, int | None]] = []
    hunk_header = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current_path and current_lines:
                hunks.append(
                    DiffHunk(
                        path=current_path,
                        old_start=current_old_start,
                        old_count=current_old_count,
                        new_start=current_new_start,
                        new_count=current_new_count,
                        lines=current_lines,
                    )
                )
                current_lines = []
            # Parse path: "diff --git a/foo.py b/foo.py" -> use new file path (b/)
            parts = line.split()
            if len(parts) >= 4:
                current_path = parts[3].removeprefix("b/")
            continue

        if line.startswith("--- ") or line.startswith("+++ "):
            if line.startswith("+++ ") and not current_path and hunks:
                pass
            elif line.startswith("+++ b/"):
                current_path = line[6:].strip()
            continue

        m = hunk_header.match(line)
        if m:
            if current_path and current_lines:
                hunks.append(
                    DiffHunk(
                        path=current_path,
                        old_start=current_old_start,
                        old_count=current_old_count,
                        new_start=current_new_start,
                        new_count=current_new_count,
                        lines=current_lines,
                    )
                )
                current_lines = []
            current_old_start = int(m.group(1))
            current_old_count = int(m.group(2) or 1)
            current_new_start = int(m.group(3))
            current_new_count = int(m.group(4) or 1)
            old_ln = current_old_start
            new_ln = current_new_start
            continue

        if not current_path:
            continue

        prefix = line[0] if line else " "
        rest = line[1:] if len(line) > 1 else ""
        if prefix == " ":
            current_lines.append((rest, old_ln, new_ln))
            old_ln += 1
            new_ln += 1
        elif prefix == "+":
            current_lines.append((rest, None, new_ln))
            new_ln += 1
        elif prefix == "-":
            current_lines.append((rest, old_ln, None))
            old_ln += 1
        elif prefix == "\\":
            current_lines.append((rest, None, None))

    if current_path and current_lines:
        hunks.append(
            DiffHunk(
                path=current_path,
                old_start=current_old_start,
                old_count=current_old_count,
                new_start=current_new_start,
                new_count=current_new_count,
                lines=current_lines,
            )
        )

    return hunks


def iter_new_lines(diff_text: str):
    """
    Yield (path, new_line, content) for each added line in the diff.
    new_line is the line number in the new file. Only yields '+' lines.
    """
    for hunk in parse_unified_diff(diff_text):
        for content, old_ln, new_ln in hunk.lines:
            if new_ln is not None and old_ln is None:
                yield (hunk.path, new_ln, content)
