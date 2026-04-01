"""Unit tests for provider-agnostic diff format adapters."""

from code_review.diff.format_adapters import bitbucket_json_diff_to_unified


def _make_bb_diff(src_path, dst_path, hunks):
    """Helper to build a minimal Bitbucket Server JSON diff dict."""
    return {
        "diffs": [
            {
                "source": {"toString": src_path} if src_path else None,
                "destination": {"toString": dst_path} if dst_path else None,
                "hunks": hunks,
            }
        ]
    }


def test_bitbucket_json_diff_to_unified_modified_file():
    data = _make_bb_diff(
        "src/Foo.java",
        "src/Foo.java",
        [
            {
                "sourceLine": 10,
                "sourceSpan": 3,
                "destinationLine": 10,
                "destinationSpan": 4,
                "segments": [
                    {"type": "CONTEXT", "lines": [{"line": "context line"}]},
                    {"type": "ADDED", "lines": [{"line": "new line"}, {"line": "another new"}]},
                    {"type": "REMOVED", "lines": [{"line": "old line"}]},
                    {"type": "CONTEXT", "lines": [{"line": "end context"}]},
                ],
            }
        ],
    )
    result = bitbucket_json_diff_to_unified(data)
    lines = result.splitlines()
    assert lines[0] == "diff --git a/src/Foo.java b/src/Foo.java"
    assert lines[1] == "--- a/src/Foo.java"
    assert lines[2] == "+++ b/src/Foo.java"
    assert lines[3] == "@@ -10,3 +10,4 @@"
    assert lines[4] == " context line"
    assert lines[5] == "+new line"
    assert lines[6] == "+another new"
    assert lines[7] == "-old line"
    assert lines[8] == " end context"


def test_bitbucket_json_diff_to_unified_new_file():
    data = _make_bb_diff(
        None,
        "src/NewFile.java",
        [
            {
                "sourceLine": 0,
                "sourceSpan": 0,
                "destinationLine": 1,
                "destinationSpan": 2,
                "segments": [
                    {"type": "ADDED", "lines": [{"line": "line 1"}, {"line": "line 2"}]},
                ],
            }
        ],
    )
    result = bitbucket_json_diff_to_unified(data)
    lines = result.splitlines()
    assert lines[0] == "diff --git a/src/NewFile.java b/src/NewFile.java"
    assert lines[1] == "--- /dev/null"
    assert lines[2] == "+++ b/src/NewFile.java"
    assert lines[3] == "@@ -0,0 +1,2 @@"
    assert lines[4] == "+line 1"
    assert lines[5] == "+line 2"


def test_bitbucket_json_diff_to_unified_deleted_file():
    data = _make_bb_diff(
        "src/Old.java",
        None,
        [
            {
                "sourceLine": 1,
                "sourceSpan": 2,
                "destinationLine": 0,
                "destinationSpan": 0,
                "segments": [
                    {"type": "REMOVED", "lines": [{"line": "gone 1"}, {"line": "gone 2"}]},
                ],
            }
        ],
    )
    result = bitbucket_json_diff_to_unified(data)
    lines = result.splitlines()
    assert lines[0] == "diff --git a/src/Old.java b/src/Old.java"
    assert lines[1] == "--- a/src/Old.java"
    assert lines[2] == "+++ /dev/null"
    assert lines[3] == "@@ -1,2 +0,0 @@"
    assert lines[4] == "-gone 1"
    assert lines[5] == "-gone 2"


def test_bitbucket_json_diff_to_unified_empty_diffs():
    assert bitbucket_json_diff_to_unified({"diffs": []}) == ""
    assert bitbucket_json_diff_to_unified({}) == ""


def test_bitbucket_json_diff_to_unified_multiple_files():
    data = {
        "diffs": [
            {
                "source": {"toString": "a.py"},
                "destination": {"toString": "a.py"},
                "hunks": [
                    {
                        "sourceLine": 1,
                        "sourceSpan": 1,
                        "destinationLine": 1,
                        "destinationSpan": 2,
                        "segments": [
                            {"type": "CONTEXT", "lines": [{"line": "ctx"}]},
                            {"type": "ADDED", "lines": [{"line": "added"}]},
                        ],
                    }
                ],
            },
            {
                "source": {"toString": "b.py"},
                "destination": {"toString": "b.py"},
                "hunks": [
                    {
                        "sourceLine": 5,
                        "sourceSpan": 1,
                        "destinationLine": 5,
                        "destinationSpan": 1,
                        "segments": [
                            {"type": "REMOVED", "lines": [{"line": "removed"}]},
                            {"type": "ADDED", "lines": [{"line": "replaced"}]},
                        ],
                    }
                ],
            },
        ]
    }
    result = bitbucket_json_diff_to_unified(data)
    assert "--- a/a.py" in result
    assert "+++ b/a.py" in result
    assert "--- a/b.py" in result
    assert "+++ b/b.py" in result
    assert "+added" in result
    assert "-removed" in result
    assert "+replaced" in result
