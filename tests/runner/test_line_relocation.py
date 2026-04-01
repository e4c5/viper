"""Tests for _relocate_findings_by_anchor: corrects misplaced finding line numbers.

When the LLM reports a finding with an anchor/fingerprint_hint that does not
match the diff content at the reported line, the runner searches nearby lines
and relocates the finding to the closest matching line.
"""

from code_review.refinement.filters.anchor_relocator import (
    relocate_findings_by_anchor as _relocate_findings_by_anchor,
)
from code_review.schemas.findings import FindingV1

# A diff with several visible lines in two files.
# foo.py hunk: new lines 8–14
# bar.py hunk: new lines 100–107
SAMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -8,4 +8,7 @@
 context_a
 context_b
+Files.writeString(entityFile, source, StandardCharsets.UTF_8);
+Files.createDirectories(packageDir);
+viewName + "." + suffix
 context_c
diff --git a/bar.py b/bar.py
--- a/bar.py
+++ b/bar.py
@@ -100,3 +100,5 @@
 old_context
+int x = doSomething();
+String name = "hello";
 end_context
"""


def _finding(
    path: str,
    line: int,
    anchor: str | None = None,
    fingerprint_hint: str | None = None,
) -> FindingV1:
    return FindingV1(
        path=path,
        line=line,
        severity="medium",
        code="test-code",
        message="test message",
        anchor=anchor,
        fingerprint_hint=fingerprint_hint,
    )


class TestRelocateFindingsByAnchor:
    """Tests for anchor-based relocation of findings."""

    def test_anchor_matches_at_reported_line(self):
        """Finding whose anchor matches at the reported line is unchanged."""
        f = _finding("foo.py", 10, anchor="Files.writeString")
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF)
        assert len(result) == 1
        assert result[0].line == 10  # unchanged

    def test_anchor_not_at_reported_line_relocated(self):
        """Finding whose anchor is NOT at reported line but found nearby → relocated."""
        # Anchor "Files.writeString" is at line 10, but finding says line 8
        f = _finding("foo.py", 8, anchor="Files.writeString")
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF)
        assert len(result) == 1
        assert result[0].line == 10  # relocated to the correct line

    def test_fingerprint_hint_used_when_no_anchor(self):
        """fingerprint_hint is used as fallback when anchor is not set."""
        f = _finding("foo.py", 8, fingerprint_hint="Files.createDirectories")
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF)
        assert len(result) == 1
        assert result[0].line == 11  # Files.createDirectories is at line 11

    def test_no_anchor_no_change(self):
        """Findings without anchor or fingerprint_hint are returned unchanged."""
        f = _finding("foo.py", 8)
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF)
        assert len(result) == 1
        assert result[0].line == 8  # no change

    def test_anchor_not_found_anywhere_no_change(self):
        """If anchor text is nowhere in the file's diff, finding is unchanged."""
        f = _finding("foo.py", 10, anchor="totallyAbsentCode")
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF)
        assert len(result) == 1
        assert result[0].line == 10  # unchanged

    def test_anchor_outside_window_no_change(self):
        """If anchor match is beyond the relocation window, finding is unchanged."""
        # viewName is at line 12, finding is at 100 — well outside the window
        f = _finding("foo.py", 100, anchor="viewName")
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF)
        assert len(result) == 1
        assert result[0].line == 100  # not relocated (outside window)

    def test_anchor_picks_closest_match(self):
        """When anchor matches multiple lines, the closest one is chosen."""
        # Both lines 10 and 11 contain "Files." but we anchor on "createDirectories"
        # which is only at line 11. Report it at line 9  →  should go to 11.
        f = _finding("foo.py", 9, anchor="createDirectories")
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF)
        assert len(result) == 1
        assert result[0].line == 11

    def test_different_file_not_cross_relocated(self):
        """Anchor search is per-file; finding in bar.py doesn't match foo.py lines."""
        f = _finding("bar.py", 101, anchor="Files.writeString")
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF)
        assert len(result) == 1
        assert result[0].line == 101  # not relocated to foo.py line

    def test_case_insensitive_matching(self):
        """Anchor matching is case insensitive."""
        f = _finding("foo.py", 8, anchor="files.writestring")
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF)
        assert len(result) == 1
        assert result[0].line == 10  # relocated despite case mismatch

    def test_empty_diff_no_change(self):
        """Empty diff → findings returned unchanged."""
        f = _finding("foo.py", 10, anchor="Files.writeString")
        result = _relocate_findings_by_anchor([f], "")
        assert len(result) == 1
        assert result[0].line == 10

    def test_empty_findings_returns_empty(self):
        """No findings → empty list."""
        result = _relocate_findings_by_anchor([], SAMPLE_DIFF)
        assert result == []

    def test_multiple_findings_mixed(self):
        """Multiple findings: some relocated, some not."""
        f1 = _finding("foo.py", 8, anchor="Files.writeString")  # relocate to 10
        f2 = _finding("foo.py", 12, anchor="viewName")  # already correct
        f3 = _finding("bar.py", 100, anchor="doSomething")  # relocate to 101

        result = _relocate_findings_by_anchor([f1, f2, f3], SAMPLE_DIFF)
        assert len(result) == 3
        assert result[0].line == 10
        assert result[1].line == 12
        assert result[2].line == 101

    def test_custom_window_size(self):
        """Relocation respects the custom window parameter."""
        # viewName is at line 12; finding at line 8 → distance=4
        f = _finding("foo.py", 8, anchor="viewName")
        # Window of 3 → too far
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF, window=3)
        assert result[0].line == 8  # not relocated (outside window)
        # Window of 5 → close enough
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF, window=5)
        assert result[0].line == 12  # relocated

    def test_file_not_in_diff_no_change(self):
        """Finding for a file not in the diff is returned unchanged."""
        f = _finding("unknown.py", 10, anchor="Files.writeString")
        result = _relocate_findings_by_anchor([f], SAMPLE_DIFF)
        assert len(result) == 1
        assert result[0].line == 10
