"""Tests for review batch sizing and diff segmentation."""

from textwrap import dedent

from code_review.batching import (
    build_review_batch_budget,
    build_review_batches,
    estimate_tokens,
    split_file_diff_into_segments,
)
from code_review.providers.base import FileInfo


def _file_diff(path: str, hunk_body: str, header: str = "@@ -1,2 +1,2 @@") -> str:
    return "\n".join(
        [
            f"diff --git a/{path} b/{path}",
            f"--- a/{path}",
            f"+++ b/{path}",
            header,
            *hunk_body.splitlines(),
        ]
    ).strip()


def test_build_review_batch_budget_reserves_output_and_prompt_capacity():
    budget = build_review_batch_budget(
        context_window_tokens=128_000,
        max_output_tokens=8_192,
        diff_budget_ratio=0.25,
    )

    assert budget.effective_input_budget_tokens == 118_784
    assert budget.effective_diff_budget_tokens == 32_000
    assert budget.prompt_budget_tokens == 86_784


def test_build_review_batches_groups_multiple_files_in_stable_order():
    diff_a = _file_diff("a.py", "-old_a\n+new_a")
    diff_b = _file_diff("b.py", "-old_b\n+new_b")
    diff_c = _file_diff("c.py", "-old_c\n+new_c")
    budget = estimate_tokens(diff_a) + estimate_tokens(diff_b) + 1

    batches = build_review_batches(
        [
            FileInfo(path="a.py", status="modified"),
            FileInfo(path="b.py", status="modified"),
            FileInfo(path="c.py", status="modified"),
        ],
        {"a.py": diff_a, "b.py": diff_b, "c.py": diff_c},
        diff_budget_tokens=budget,
    )

    assert [batch.paths for batch in batches] == [("a.py", "b.py"), ("c.py",)]


def test_split_file_diff_into_segments_splits_on_hunk_boundaries():
    diff_text = _file_diff(
        "service.py",
        dedent(
            """\
            -before_one
            +after_one
            @@ -20,2 +20,2 @@
            -before_two
            +after_two
            """
        ).strip(),
    )
    budget = estimate_tokens(_file_diff("service.py", "-before_one\n+after_one")) + 2

    segments = split_file_diff_into_segments(
        "service.py",
        diff_text,
        segment_budget_tokens=budget,
    )

    assert len(segments) == 2
    assert all(segment.total_segments == 2 for segment in segments)
    assert all(segment.split_strategy == "hunk" for segment in segments)
    assert "@@ -1,2 +1,2 @@" in segments[0].diff_text
    assert "@@ -20,2 +20,2 @@" in segments[1].diff_text


def test_split_file_diff_into_segments_splits_oversized_single_hunk():
    diff_text = _file_diff(
        "big.py",
        dedent(
            """\
             context_1
             context_2
            -old_3
            +new_3
             context_4
             context_5
            -old_6
            +new_6
             context_7
             context_8
            """
        ).strip(),
        header="@@ -10,8 +10,8 @@",
    )

    segment_budget = (
        estimate_tokens(
            _file_diff(
                "big.py",
                " context_1\n context_2\n-old_3\n+new_3",
                header="@@ -10,4 +10,4 @@",
            )
        )
        + 1
    )

    segments = split_file_diff_into_segments(
        "big.py",
        diff_text,
        segment_budget_tokens=segment_budget,
    )

    assert len(segments) > 1
    assert all(segment.split_strategy == "intra_hunk" for segment in segments)
    assert all(segment.total_segments == len(segments) for segment in segments)
    assert all(segment.estimated_tokens <= segment_budget for segment in segments)
    assert all("@@ " in segment.diff_text for segment in segments)


def test_split_file_diff_into_segments_splits_oversized_single_hunk_line():
    long_line = "x" * 240
    diff_text = _file_diff("big.py", f"+{long_line}", header="@@ -1,0 +1,1 @@")
    segment_budget = estimate_tokens(diff_text) // 3

    segments = split_file_diff_into_segments(
        "big.py",
        diff_text,
        segment_budget_tokens=segment_budget,
    )

    assert len(segments) > 1
    assert all(segment.split_strategy == "intra_hunk" for segment in segments)
    assert all(segment.estimated_tokens <= segment_budget for segment in segments)
    assert all("@@ -1,0 +1,1 @@" in segment.diff_text for segment in segments)


def test_split_file_diff_into_segments_falls_back_when_no_hunks_exist():
    diff_text = dedent(
        """\
        diff --git a/binary.dat b/binary.dat
        Binary files a/binary.dat and b/binary.dat differ
        """
    ).strip()

    segments = split_file_diff_into_segments(
        "binary.dat",
        diff_text,
        segment_budget_tokens=10,
    )

    assert len(segments) >= 1
    assert all(segment.split_strategy == "line_fallback" for segment in segments)


def test_split_file_diff_into_segments_splits_oversized_plain_text_line():
    long_metadata = "Binary files differ: " + ("x" * 240)
    diff_text = dedent(
        f"""\
        diff --git a/binary.dat b/binary.dat
        {long_metadata}
        """
    ).strip()
    segment_budget = estimate_tokens(long_metadata) // 3

    segments = split_file_diff_into_segments(
        "binary.dat",
        diff_text,
        segment_budget_tokens=segment_budget,
    )

    assert len(segments) > 1
    assert all(segment.split_strategy == "line_fallback" for segment in segments)
    assert all(segment.estimated_tokens <= segment_budget for segment in segments)
