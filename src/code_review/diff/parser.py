"""Parse unified diff format for line positions."""

import re
from dataclasses import dataclass, field
from re import Match

# Module-level constant avoids the regex pattern lookup cost on every call.
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class DiffHunk:
    """A hunk of changed lines in a file."""

    path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, int | None, int | None]]  # (content, old_line, new_line)


@dataclass
class _UnifiedDiffParseState:
    """Mutable state while scanning a unified diff (keeps ``parse_unified_diff`` shallow)."""

    hunks: list[DiffHunk] = field(default_factory=list)
    current_path: str = ""
    current_old_start: int = 0
    current_old_count: int = 0
    current_new_start: int = 0
    current_new_count: int = 0
    current_lines: list[tuple[str, int | None, int | None]] = field(default_factory=list)
    old_ln: int = 0
    new_ln: int = 0

    def flush_current_hunk(self) -> None:
        if self.current_path and self.current_lines:
            self.hunks.append(
                DiffHunk(
                    path=self.current_path,
                    old_start=self.current_old_start,
                    old_count=self.current_old_count,
                    new_start=self.current_new_start,
                    new_count=self.current_new_count,
                    lines=self.current_lines,
                )
            )
        self.current_lines = []

    def on_diff_git_line(self, line: str) -> None:
        self.flush_current_hunk()
        parts = line.split()
        if len(parts) >= 4:
            self.current_path = parts[3].removeprefix("b/")

    def on_plus_minus_header_line(self, line: str) -> None:
        if line.startswith("+++ b/"):
            self.current_path = line[6:].strip()

    def on_hunk_header_line(self, m: Match[str]) -> None:
        self.flush_current_hunk()
        self.current_old_start = int(m.group(1))
        self.current_old_count = int(m.group(2) or 1)
        self.current_new_start = int(m.group(3))
        self.current_new_count = int(m.group(4) or 1)
        self.old_ln = self.current_old_start
        self.new_ln = self.current_new_start

    def on_hunk_body_line(self, line: str) -> None:
        if not self.current_path:
            return
        prefix = line[0] if line else " "
        rest = line[1:] if len(line) > 1 else ""
        if prefix == " ":
            self.current_lines.append((rest, self.old_ln, self.new_ln))
            self.old_ln += 1
            self.new_ln += 1
        elif prefix == "+":
            self.current_lines.append((rest, None, self.new_ln))
            self.new_ln += 1
        elif prefix == "-":
            self.current_lines.append((rest, self.old_ln, None))
            self.old_ln += 1
        elif prefix == "\\":
            self.current_lines.append((rest, None, None))


def parse_unified_diff(diff_text: str) -> list[DiffHunk]:
    """
    Parse unified diff text into a list of DiffHunk.
    Each hunk contains lines with (content, old_line, new_line).
    old_line/new_line are None for context lines in add/remove-only hunks.
    """
    st = _UnifiedDiffParseState()
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            st.on_diff_git_line(line)
        elif line.startswith("--- ") or line.startswith("+++ "):
            st.on_plus_minus_header_line(line)
        elif m := _HUNK_HEADER_RE.match(line):
            st.on_hunk_header_line(m)
        else:
            st.on_hunk_body_line(line)
    st.flush_current_hunk()
    return st.hunks


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
    ``n:`` where *n* is the absolute new-file line number.  Removed lines
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
        100:  context_line_100
        -old_line_101
        101:+new_line_101
        102:  context_line_102

    The updated agent instructions tell the model to use the ``n:``
    annotation as the ``line`` value in findings, rather than inferring it
    from hunk arithmetic.
    """
    if not diff_text:
        return diff_text

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

        m = _HUNK_HEADER_RE.match(line)
        if m:
            new_ln = int(m.group(3))
            in_hunk = True
            result_lines.append(line)
            continue

        if not in_hunk:
            result_lines.append(line)
            continue

        prefix = line[0] if line else " "
        if prefix in (" ", "+"):
            result_lines.append(f"{new_ln}:" + line)
            new_ln += 1
        else:
            # Removed lines (-), no-newline markers (\), and any other prefix
            # have no new-file line number — pass through unchanged.
            result_lines.append(line)

    return "\n".join(result_lines)
