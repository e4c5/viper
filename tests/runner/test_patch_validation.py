"""Tests for _validate_suggested_patches: strips misplaced patches before posting.

This guardrail catches the common LLM error of hallucinating a suggested_patch for a
different piece of code while naming a visible line in the diff (which passes the
line-visibility guardrail). Without this check the patch would be rendered as a
suggestion block replacing the wrong code.
"""

from unittest.mock import patch

from code_review.refinement.filters.patch_validator import (
    validate_suggested_patches as _validate_suggested_patches,
)
from code_review.schemas.findings import FindingV1

# A minimal diff with two files.
# File foo.py: line 10 contains "if isId", line 11 contains "sb.append(value)"
# File bar.py: line 5 contains "int x = 0;"
SAMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -8,4 +8,5 @@
 context_a
 context_b
+if isId || !nullable {
+sb.append(value);
 context_c
diff --git a/bar.py b/bar.py
--- a/bar.py
+++ b/bar.py
@@ -4,3 +4,4 @@
 ctx1
+int x = 0;
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


def test_valid_patch_kept():
    """A patch that shares tokens with the diff line should be preserved."""
    # Line 10 is "if isId || !nullable {"; patch replaces the same token
    f = _finding("foo.py", 10, "if isId || nullable {")
    result = _validate_suggested_patches([f], SAMPLE_DIFF)
    assert len(result) == 1
    assert result[0].suggested_patch == "if isId || nullable {"


def test_misplaced_patch_stripped():
    """A patch with no token overlap with the diff line should have its patch cleared."""
    # Line 10 is "if isId || !nullable {"; patch is for System.nanoTime() — no overlap
    f = _finding(
        "foo.py", 10, '.append(System.nanoTime()).append("* author=\\"antikythera\\"\\n");'
    )
    result = _validate_suggested_patches([f], SAMPLE_DIFF)
    assert len(result) == 1
    assert result[0].suggested_patch is None, (
        "Patch referencing completely unrelated code should be stripped"
    )


def test_misplaced_patch_warning_does_not_log_raw_code():
    """Misplaced patch warning should log metadata only, not raw source content."""
    suggested_patch = '.append(System.nanoTime()).append("* author=\\"antikythera\\"\\n");'
    f = _finding("foo.py", 10, suggested_patch)

    with patch("code_review.refinement.filters.patch_validator.logger.warning") as mock_warning:
        result = _validate_suggested_patches([f], SAMPLE_DIFF)

    assert result[0].suggested_patch is None
    mock_warning.assert_called_once()
    message = mock_warning.call_args.args[0] % mock_warning.call_args.args[1:]
    assert "foo.py:10" in message
    assert "overlap_tokens=0" in message
    assert "patch_tokens=" in message
    assert "actual_tokens=" in message
    assert suggested_patch not in message
    assert "if isId || !nullable {" not in message


def test_no_patch_unchanged():
    """Findings without a suggested_patch are returned unchanged."""
    f = _finding("foo.py", 10, None)
    result = _validate_suggested_patches([f], SAMPLE_DIFF)
    assert result[0].suggested_patch is None


def test_empty_diff_keeps_all():
    """No diff available → findings returned unchanged (no false positives)."""
    f = _finding("foo.py", 10, "totally unrelated patch")
    result = _validate_suggested_patches([f], "")
    assert result[0].suggested_patch == "totally unrelated patch"


def test_line_not_in_diff_keeps_patch():
    """If the line is not in the diff index (e.g., outside hunk), do not strip."""
    f = _finding("foo.py", 99, "some patch")
    result = _validate_suggested_patches([f], SAMPLE_DIFF)
    assert result[0].suggested_patch == "some patch"


def test_short_actual_line_keeps_patch():
    """A very short actual line (e.g. single brace) is not used to reject a patch."""
    diff = """\
diff --git a/brace.py b/brace.py
--- a/brace.py
+++ b/brace.py
@@ -1,2 +1,3 @@
 {
+    doSomething();
 }
"""
    # Line 1 has content "{" (len=1 <= 5) — the patch must be kept regardless of overlap.
    f = _finding("brace.py", 1, "{ /* open block */")
    result = _validate_suggested_patches([f], diff)
    assert result[0].suggested_patch is not None


def test_multiple_findings_mixed():
    """Multiple findings: valid patch kept, invalid patch stripped, no-patch unchanged."""
    f_valid = _finding("foo.py", 10, "if isId && nullable {")
    f_bad = _finding("foo.py", 10, ".append(System.nanoTime());")
    f_none = _finding("bar.py", 5, None)

    result = _validate_suggested_patches([f_valid, f_bad, f_none], SAMPLE_DIFF)

    assert result[0].suggested_patch == "if isId && nullable {"
    assert result[1].suggested_patch is None
    assert result[2].suggested_patch is None
