"""Golden tests: sample diffs + expected position mapping (Phase 5)."""

import pytest

from code_review.diff.parser import parse_unified_diff
from code_review.diff.position import get_commentable_positions

# Golden samples: (diff_text, list of (path, new_start, new_count), list of (path, line) commentable)
GOLDEN_SAMPLES = [
    (
        """diff --git a/foo.py b/foo.py
index abc..def 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 line1
 line2
+new line
 line3
""",
        [("foo.py", 1, 4)],
        [("foo.py", 3)],
    ),
    (
        """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 x
+y
 z
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1,1 +1,2 @@
 a
+b
""",
        [("a.py", 1, 3), ("b.py", 1, 2)],
        [("a.py", 2), ("b.py", 2)],
    ),
    (
        """diff --git a/b/bar.py b/b/bar.py
--- a/b/bar.py
+++ b/b/bar.py
@@ -1,1 +1,2 @@
 x
+y
""",
        [("b/bar.py", 1, 2)],
        [("b/bar.py", 2)],
    ),
]


@pytest.mark.parametrize("diff_text,expected_hunks,expected_positions", GOLDEN_SAMPLES)
def test_golden_parse_unified_diff(diff_text, expected_hunks, expected_positions):
    """Parse golden diff and assert hunk paths and new_start/new_count."""
    hunks = parse_unified_diff(diff_text)
    assert len(hunks) == len(expected_hunks)
    for hunk, (path, new_start, new_count) in zip(hunks, expected_hunks):
        assert hunk.path == path
        assert hunk.new_start == new_start
        assert hunk.new_count == new_count


@pytest.mark.parametrize("diff_text,expected_hunks,expected_positions", GOLDEN_SAMPLES)
def test_golden_commentable_positions(diff_text, expected_hunks, expected_positions):
    """Golden diff produces expected commentable (path, line_in_new_file) positions."""
    positions = get_commentable_positions(diff_text)
    path_lines = {(p.path, p.line_in_new_file) for p in positions}
    for path, line in expected_positions:
        assert (path, line) in path_lines, f"Expected ({path}, {line}) in {path_lines}"
