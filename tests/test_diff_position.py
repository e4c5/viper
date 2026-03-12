"""Tests for diff position and fingerprint."""

from code_review.diff import (
    build_fingerprint,
    content_hash,
    get_commentable_positions,
    get_diff_hunk_for_line,
    normalize_anchor,
    position_for_line,
)


def test_get_commentable_positions():
    diff = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 x
+y
 z
"""
    pos = get_commentable_positions(diff)
    assert len(pos) >= 1
    paths_lines = [(p.path, p.line_in_new_file) for p in pos]
    assert ("foo.py", 2) in paths_lines


def test_position_for_line():
    # Context lines in unified diff start with space
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n a\n+b\n"
    p = position_for_line(diff, "a.py", 2)
    assert p is not None
    assert p.line_in_new_file == 2
    assert p.path == "a.py"
    assert position_for_line(diff, "a.py", 99) is None


def test_get_diff_hunk_for_line():
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n a\n+b\n"
    hunk = get_diff_hunk_for_line(diff, "a.py", 2)
    assert hunk is not None
    assert "@@" in hunk
    assert "+b" in hunk
    assert get_diff_hunk_for_line(diff, "a.py", 99) is None
    assert get_diff_hunk_for_line(diff, "other.py", 2) is None
    # path with leading slash normalizes
    assert get_diff_hunk_for_line(diff, "/a.py", 2) is not None


def test_get_diff_hunk_for_line_includes_deletion_and_context():
    """Cover all prefix branches: space, plus, minus."""
    diff = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n+++ b/f.py\n"
        "@@ -1,4 +1,3 @@\n"
        " context\n"
        "-deleted\n"
        " more\n"
        "+added\n"
    )
    # Line 2 in new file is " more" (context) -> space prefix
    hunk = get_diff_hunk_for_line(diff, "f.py", 2)
    assert hunk is not None
    assert " more" in hunk
    # Line 3 in new file is "+added" -> plus prefix
    hunk3 = get_diff_hunk_for_line(diff, "f.py", 3)
    assert hunk3 is not None
    assert "+added" in hunk3


def test_get_diff_hunk_for_line_no_newline_at_end():
    """Cover backslash prefix (\\ No newline at end of file)."""
    diff = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n+++ b/f.py\n"
        "@@ -1,2 +1,2 @@\n"
        " a\n"
        "+b\n"
        "\\ No newline at end of file\n"
    )
    hunk = get_diff_hunk_for_line(diff, "f.py", 2)
    assert hunk is not None
    assert "No newline" in hunk or "+b" in hunk


def test_normalize_anchor():
    assert normalize_anchor("  foo  bar  ") == "foo bar"
    assert normalize_anchor("x\ty\nz") == "x y z"


def test_content_hash():
    h = content_hash("hello")
    assert len(h) == 16
    assert h == content_hash("hello")
    assert h != content_hash("world")


def test_build_fingerprint():
    fp = build_fingerprint("a.py", "abc123", "unused-var", "x = 1")
    assert len(fp) == 24
    assert fp == build_fingerprint("a.py", "abc123", "unused-var", "x = 1")
