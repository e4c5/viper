"""Tests for diff parser."""

from code_review.diff import annotate_diff_with_line_numbers, iter_new_lines, parse_unified_diff


def test_parse_simple_diff():
    diff = """diff --git a/foo.py b/foo.py
index abc..def 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 line1
 line2
+new line
 line3
"""
    hunks = parse_unified_diff(diff)
    assert len(hunks) == 1
    assert hunks[0].path == "foo.py"
    assert hunks[0].new_start == 1
    lines = hunks[0].lines
    assert len(lines) == 4
    assert lines[2] == ("new line", None, 3)


def test_iter_new_lines():
    diff = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 x
+y
 z
"""
    items = list(iter_new_lines(diff))
    assert items == [("foo.py", 2, "y")]


def test_iter_new_lines_includes_blank_lines():
    diff = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,4 @@
 x
+
+z
"""
    items = list(iter_new_lines(diff))
    assert items == [("foo.py", 2, ""), ("foo.py", 3, "z")]


def test_parse_path_starting_with_b():
    """Path b/bar.py must not be corrupted by stripping leading 'b' chars."""
    diff = """diff --git a/b/bar.py b/b/bar.py
--- a/b/bar.py
+++ b/b/bar.py
@@ -1,1 +1,2 @@
 x
+y
"""
    hunks = parse_unified_diff(diff)
    assert len(hunks) == 1
    assert hunks[0].path == "b/bar.py"


def test_parse_multi_file():
    diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,1 +1,1 @@
-x
+y
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1,1 +1,2 @@
 a
+b
"""
    hunks = parse_unified_diff(diff)
    assert len(hunks) == 2
    assert hunks[0].path == "a.py"
    assert hunks[1].path == "b.py"


# ---------------------------------------------------------------------------
# annotate_diff_with_line_numbers
# ---------------------------------------------------------------------------


def test_annotate_diff_context_and_added_lines():
    """Context (' ') and added ('+') lines get <L{n}> annotations; removed lines do not."""
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n+++ b/foo.py\n"
        "@@ -1,3 +1,3 @@\n"
        " context_line\n"
        "-removed_line\n"
        "+added_line\n"
    )
    out = annotate_diff_with_line_numbers(diff)
    lines = out.splitlines()
    # Header lines are unchanged
    assert lines[0] == "diff --git a/foo.py b/foo.py"
    assert lines[1] == "--- a/foo.py"
    assert lines[2] == "+++ b/foo.py"
    assert lines[3].startswith("@@")
    # context_line is at new-file line 1
    assert "<L1> context_line" in out
    # removed_line has no annotation — check the actual line containing it
    assert all("<L" not in ln for ln in out.splitlines() if "-removed_line" in ln)
    # added_line is at new-file line 2 (context incremented to 1, then added = 2)
    assert "<L2>+added_line" in out


def test_annotate_diff_correct_line_numbers_with_deletions():
    """When deletions precede added lines, annotations must account for them correctly."""
    diff = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n+++ b/f.py\n"
        "@@ -10,5 +10,4 @@\n"
        " ctx_a\n"
        "-old_b\n"
        "-old_c\n"
        "+new_b\n"
        " ctx_d\n"
    )
    out = annotate_diff_with_line_numbers(diff)
    # ctx_a is at new-file line 10
    assert "<L10> ctx_a" in out
    # old_b and old_c have no new-file line — just raw '-' lines
    assert "-old_b" in out
    assert "-old_c" in out
    # new_b is at new-file line 11 (10 used by ctx_a, old lines don't count)
    assert "<L11>+new_b" in out
    # ctx_d is at new-file line 12
    assert "<L12> ctx_d" in out


def test_annotate_diff_empty_string():
    """Empty input returns empty string without error."""
    assert annotate_diff_with_line_numbers("") == ""


def test_annotate_diff_file_header_lines_unchanged():
    """diff --git, index, ---/+++ lines pass through without modification."""
    diff = (
        "diff --git a/a.py b/a.py\n"
        "index abc123..def456 100644\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,1 +1,1 @@\n"
        "+new_content\n"
    )
    out = annotate_diff_with_line_numbers(diff)
    assert "diff --git a/a.py b/a.py" in out
    assert "index abc123..def456 100644" in out
    assert "--- a/a.py" in out
    assert "+++ b/a.py" in out


def test_annotate_diff_multi_hunk():
    """Each hunk resets the line counter from its own new_start."""
    diff = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n+++ b/f.py\n"
        "@@ -1,2 +1,2 @@\n"
        " lineA\n"
        "+lineB\n"
        "@@ -10,2 +10,3 @@\n"
        " lineX\n"
        "+lineY\n"
        " lineZ\n"
    )
    out = annotate_diff_with_line_numbers(diff)
    assert "<L1> lineA" in out
    assert "<L2>+lineB" in out
    assert "<L10> lineX" in out
    assert "<L11>+lineY" in out
    assert "<L12> lineZ" in out


def test_annotate_diff_multi_file():
    """Multiple files in the diff are all annotated independently."""
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n+++ b/a.py\n"
        "@@ -1,1 +1,1 @@\n"
        "+a_new\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n+++ b/b.py\n"
        "@@ -5,1 +5,1 @@\n"
        "+b_new\n"
    )
    out = annotate_diff_with_line_numbers(diff)
    assert "<L1>+a_new" in out
    assert "<L5>+b_new" in out


def test_annotate_diff_roundtrip_line_numbers_match_parser():
    """Line numbers in annotations must match what parse_unified_diff reports."""
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n+++ b/foo.py\n"
        "@@ -8,5 +8,6 @@\n"
        " context_8\n"
        " context_9\n"
        "-old_10\n"
        "+new_10\n"
        "+new_11\n"
        " context_12\n"
        " context_13\n"
    )
    out = annotate_diff_with_line_numbers(diff)

    # Collect all annotations from output
    annotated: dict[int, str] = {}
    for line in out.splitlines():
        if line.startswith("<L"):
            end = line.index(">")
            n = int(line[2:end])
            annotated[n] = line[end + 1:]

    # Collect expected new-file lines from parser
    expected: dict[int, str] = {}
    from code_review.diff.parser import parse_unified_diff as _parse
    for hunk in _parse(diff):
        for content, old_ln, new_ln in hunk.lines:
            if new_ln is not None:
                prefix = " " if old_ln is not None else "+"
                expected[new_ln] = prefix + content

    assert annotated == expected
