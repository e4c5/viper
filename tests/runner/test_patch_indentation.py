"""Tests for normalize_patch_indentation: re-indents patches missing leading whitespace."""

from code_review.refinement.filters.patch_indentation import normalize_patch_indentation
from code_review.schemas.findings import FindingV1

# Diff in which:
#   foo.py line 9 is inside a class method body indented with 8 spaces.
#   foo.py line 8 is a def line with 4-space indent.
#   bar.py line 4 is top-level (no indentation).
#   baz.py line 2 is indented with a tab.
SAMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -8,5 +8,5 @@
     def method(self):
-        return old_value
+        return new_value
     def other(self):
diff --git a/bar.py b/bar.py
--- a/bar.py
+++ b/bar.py
@@ -3,4 +3,4 @@
 ctx
-old_top_level = 1
+top_level = 1
 ctx2
diff --git a/baz.py b/baz.py
--- a/baz.py
+++ b/baz.py
@@ -1,4 +1,4 @@
 ctx
-\told = "x"
+\tnew = "y"
 ctx2
"""


def _finding(path: str, line: int, patch: str | None) -> FindingV1:
    return FindingV1(
        path=path,
        line=line,
        severity="low",
        code="test-code",
        message="test message",
        suggested_patch=patch,
    )


# ---------------------------------------------------------------------------
# Core correction cases
# ---------------------------------------------------------------------------


def test_missing_indent_single_line_fixed():
    """A patch without leading spaces is fixed to match the 8-space diff line.

    The added line ``        return new_value`` is at new-file line 9 (8 spaces of indent).
    """
    f = _finding("foo.py", 9, "return fixed_value")
    result = normalize_patch_indentation([f], SAMPLE_DIFF)
    assert result[0].suggested_patch == "        return fixed_value"


def test_missing_indent_four_space_line_fixed():
    """A 4-space indented line (the def line at line 8) also gets its indent restored."""
    f = _finding("foo.py", 8, "def renamed_method(self):")
    result = normalize_patch_indentation([f], SAMPLE_DIFF)
    assert result[0].suggested_patch == "    def renamed_method(self):"


def test_missing_indent_multiline_fixed():
    """All non-empty lines of a multi-line patch get the missing indent prefixed."""
    # Line 9 of foo.py has 8-space indent.
    f = _finding("foo.py", 9, "if cond:\n    do_something()")
    result = normalize_patch_indentation([f], SAMPLE_DIFF)
    assert result[0].suggested_patch == "        if cond:\n            do_something()"


def test_tab_indent_fixed():
    """Tab-indented lines are fixed just like space-indented ones."""
    # Line 2 of baz.py has a leading tab.
    f = _finding("baz.py", 2, 'new = "z"')
    result = normalize_patch_indentation([f], SAMPLE_DIFF)
    assert result[0].suggested_patch == '\tnew = "z"'


# ---------------------------------------------------------------------------
# Cases that must NOT be modified
# ---------------------------------------------------------------------------


def test_already_indented_patch_unchanged():
    """If the patch already carries leading whitespace, leave it alone."""
    f = _finding("foo.py", 9, "        return already_correct")
    result = normalize_patch_indentation([f], SAMPLE_DIFF)
    assert result[0].suggested_patch == "        return already_correct"


def test_top_level_patch_unchanged():
    """Top-level code (no indent on diff line) should not be altered."""
    # Line 4 of bar.py has no indentation.
    f = _finding("bar.py", 4, "top_level = 2")
    result = normalize_patch_indentation([f], SAMPLE_DIFF)
    assert result[0].suggested_patch == "top_level = 2"


def test_no_patch_unchanged():
    """Findings without a suggested_patch are returned unchanged."""
    f = _finding("foo.py", 9, None)
    result = normalize_patch_indentation([f], SAMPLE_DIFF)
    assert result[0].suggested_patch is None


def test_empty_diff_unchanged():
    """When no diff is provided, all findings are returned as-is."""
    f = _finding("foo.py", 9, "return x")
    result = normalize_patch_indentation([f], "")
    assert result[0].suggested_patch == "return x"


def test_line_not_in_diff_unchanged():
    """If the line is not in the diff index, the patch is not modified."""
    f = _finding("foo.py", 999, "return x")
    result = normalize_patch_indentation([f], SAMPLE_DIFF)
    assert result[0].suggested_patch == "return x"


def test_blank_lines_in_patch_not_indented():
    """Blank lines inside a multi-line patch are not given a spurious indent."""
    # Line 9 of foo.py has 8-space indent.
    f = _finding("foo.py", 9, "if cond:\n\n    do_something()")
    result = normalize_patch_indentation([f], SAMPLE_DIFF)
    # Middle blank line must remain blank (no trailing spaces added).
    lines = result[0].suggested_patch.splitlines()
    assert lines[1] == ""


def test_multiple_findings_mixed():
    """Batch: one fixed, one already correct, one no-patch."""
    f_needs_fix = _finding("foo.py", 9, "return bad")
    f_ok = _finding("foo.py", 9, "        return good")
    f_none = _finding("bar.py", 4, None)

    result = normalize_patch_indentation([f_needs_fix, f_ok, f_none], SAMPLE_DIFF)

    assert result[0].suggested_patch == "        return bad"
    assert result[1].suggested_patch == "        return good"
    assert result[2].suggested_patch is None
