"""Tests for diff line index builders."""

from code_review.diff.line_index import build_diff_line_index, build_per_file_line_index


def test_line_indexes_preserve_whitespace_in_diff_content() -> None:
    diff_text = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,2 @@
-old
+  padded value  
 trailing   
"""

    line_index = build_diff_line_index(diff_text)
    per_file_index = build_per_file_line_index(diff_text)

    assert line_index[("foo.py", 1)] == "  padded value  "
    assert line_index[("foo.py", 2)] == "trailing   "
    assert per_file_index["foo.py"][1] == "  padded value  "
    assert per_file_index["foo.py"][2] == "trailing   "
