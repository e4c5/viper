"""Tests for diff parser."""

import pytest

from code_review.diff import parse_unified_diff, DiffHunk, iter_new_lines


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
