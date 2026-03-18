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
    def _flush_current_hunk() -> None:
        nonlocal current_lines, current_path, current_old_start, current_old_count
        nonlocal current_new_start, current_new_count
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

    hunks: list[DiffHunk] = []
    current_path = ""
    current_old_start = 0
    current_old_count = 0
    current_new_start = 0
    current_new_count = 0
    current_lines: list[tuple[str, int | None, int | None]] = []
    old_ln = 0
    new_ln = 0
    hunk_header = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            _flush_current_hunk()
            # Parse path: "diff --git a/foo.py b/foo.py" -> use new file path (b/)
            parts = line.split()
            if len(parts) >= 4:
                current_path = parts[3].removeprefix("b/")
            continue

        if line.startswith("--- ") or line.startswith("+++ "):
            # Header lines can update the path for non-git diffs; the existing
            # current_path is kept when appropriate.
            if line.startswith("+++ b/"):
                current_path = line[6:].strip()
            continue

        m = hunk_header.match(line)
        if m:
            _flush_current_hunk()
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

    _flush_current_hunk()

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


def annotate_diff_with_line_numbers(diff_text: str) -> str:
    """Annotate a unified diff with explicit new-file line numbers.

    For each line visible in the new file ('+' added or ' ' context), prepend
    ``<L{n}>`` where *n* is the absolute new-file line number.  Removed lines
    ('-' prefix) are kept as-is without annotation because they do not exist
    in the new file.

    This makes line numbers explicit for the LLM so it does not have to
    compute positions by counting lines relative to hunk headers — a
    calculation that models frequently get wrong, especially when deletions
    precede the line of interest.

    Example input::

        @@ -100,4 +100,4 @@
         context_line_100
        -old_line_101
        +new_line_101
         context_line_102

    Example output::

        @@ -100,4 +100,4 @@
        <L100>  context_line_100
        -old_line_101
        <L101> +new_line_101
        <L102>  context_line_102

    The updated agent instructions tell the model to use the ``<L{n}>``
    annotation as the ``line`` value in findings, rather than inferring it
    from hunk arithmetic.
    """
    if not diff_text:
        return diff_text

    _hunk_header = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    result_lines: list[str] = []
    in_hunk = False
    new_ln = 0

    for line in diff_text.splitlines():
        # File-level header lines — pass through unchanged.
        if (
            line.startswith("diff --git ")
            or line.startswith("index ")
            or line.startswith("--- ")
            or line.startswith("+++ ")
        ):
            in_hunk = False
            result_lines.append(line)
            continue

        m = _hunk_header.match(line)
        if m:
            new_ln = int(m.group(3))
            in_hunk = True
            result_lines.append(line)
            continue

        if not in_hunk:
            result_lines.append(line)
            continue

        prefix = line[0] if line else " "
        if prefix == " ":
            result_lines.append(f"<L{new_ln}>" + line)
            new_ln += 1
        elif prefix == "+":
            result_lines.append(f"<L{new_ln}>" + line)
            new_ln += 1
        elif prefix == "-":
            # Removed line — no new-file line number.
            result_lines.append(line)
        else:
            # '\' (no-newline marker) and any unexpected prefix — pass through.
            result_lines.append(line)

    return "\n".join(result_lines)
