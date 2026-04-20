"""Helpers for multi-file review batch sizing and construction."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from code_review.diff.parser import DiffHunk, parse_unified_diff
from code_review.diff.utils import estimate_tokens
from code_review.providers.base import FileInfo

_HUNK_HEADER_RE = re.compile(r"^@@ ")


@dataclass(frozen=True)
class ReviewBatchBudget:
    """Token budget plan for one prepared review batch."""

    model_context_window_tokens: int
    max_output_tokens_reserved: int
    prompt_token_reserve: int
    context_brief_token_reserve: int
    safety_margin_tokens: int
    diff_budget_ratio: float
    effective_input_budget_tokens: int
    effective_diff_budget_tokens: int
    prompt_budget_tokens: int


@dataclass(frozen=True)
class ReviewSegment:
    """A reviewable diff segment, possibly one slice of a larger file."""

    path: str
    diff_text: str
    estimated_tokens: int
    segment_index: int
    total_segments: int
    split_strategy: Literal["whole_file", "hunk", "intra_hunk", "line_fallback"]


@dataclass(frozen=True)
class ReviewBatch:
    """Prepared batch of file diff segments for one model review step."""

    batch_index: int
    estimated_tokens: int
    segments: tuple[ReviewSegment, ...]
    paths: tuple[str, ...]


def build_review_batch_budget(
    *,
    context_window_tokens: int,
    max_output_tokens: int,
    diff_budget_ratio: float,
    prompt_token_reserve: int = 2_048,
    context_brief_token_reserve: int = 0,
    safety_margin_tokens: int = 1_024,
) -> ReviewBatchBudget:
    """Calculate the effective diff budget for one review batch."""
    if context_window_tokens <= 0:
        raise ValueError("context_window_tokens must be positive")
    if max_output_tokens < 0:
        raise ValueError("max_output_tokens must be non-negative")
    if not 0 < diff_budget_ratio <= 1:
        raise ValueError("diff_budget_ratio must be in the range (0, 1]")

    effective_input_budget = max(
        0,
        context_window_tokens - max_output_tokens - safety_margin_tokens,
    )
    requested_diff_budget = int(context_window_tokens * diff_budget_ratio)
    max_diff_budget = max(
        0,
        effective_input_budget
        - prompt_token_reserve
        - context_brief_token_reserve,
    )
    effective_diff_budget = min(requested_diff_budget, max_diff_budget)
    prompt_budget_tokens = max(0, effective_input_budget - effective_diff_budget)

    return ReviewBatchBudget(
        model_context_window_tokens=context_window_tokens,
        max_output_tokens_reserved=max_output_tokens,
        prompt_token_reserve=prompt_token_reserve,
        context_brief_token_reserve=context_brief_token_reserve,
        safety_margin_tokens=safety_margin_tokens,
        diff_budget_ratio=diff_budget_ratio,
        effective_input_budget_tokens=effective_input_budget,
        effective_diff_budget_tokens=effective_diff_budget,
        prompt_budget_tokens=prompt_budget_tokens,
    )


def build_review_batches(
    files: Sequence[FileInfo],
    diff_by_path: Mapping[str, str],
    *,
    diff_budget_tokens: int,
) -> list[ReviewBatch]:
    """Group ordered file diffs into ordered review batches."""
    if diff_budget_tokens <= 0:
        raise ValueError("diff_budget_tokens must be positive")

    segments: list[ReviewSegment] = []
    for file_info in files:
        path = (file_info.path or "").strip()
        if not path:
            continue
        diff_text = (diff_by_path.get(path) or "").strip()
        if not diff_text:
            continue
        segments.extend(
            split_file_diff_into_segments(
                path,
                diff_text,
                segment_budget_tokens=diff_budget_tokens,
            )
        )

    batches: list[ReviewBatch] = []
    current_segments: list[ReviewSegment] = []
    current_tokens = 0

    for segment in segments:
        if current_segments and current_tokens + segment.estimated_tokens > diff_budget_tokens:
            batches.append(_build_batch(len(batches), current_segments, current_tokens))
            current_segments = []
            current_tokens = 0
        current_segments.append(segment)
        current_tokens += segment.estimated_tokens

    if current_segments:
        batches.append(_build_batch(len(batches), current_segments, current_tokens))

    return batches


def split_file_diff_into_segments(
    path: str,
    diff_text: str,
    *,
    segment_budget_tokens: int,
) -> list[ReviewSegment]:
    """Split one file diff into budget-fitting review segments."""
    if segment_budget_tokens <= 0:
        raise ValueError("segment_budget_tokens must be positive")
    normalized_path = (path or "").strip()
    normalized_diff = (diff_text or "").strip()
    if not normalized_path or not normalized_diff:
        return []

    whole_file_tokens = estimate_tokens(normalized_diff)
    if whole_file_tokens <= segment_budget_tokens:
        return [
            ReviewSegment(
                path=normalized_path,
                diff_text=normalized_diff,
                estimated_tokens=whole_file_tokens,
                segment_index=0,
                total_segments=1,
                split_strategy="whole_file",
            )
        ]

    hunks = [h for h in parse_unified_diff(normalized_diff) if h.path == normalized_path]
    if not hunks:
        return _finalize_segments(
            normalized_path,
            _split_plain_text_segment(normalized_diff, segment_budget_tokens),
            split_strategy="line_fallback",
        )

    header_lines = _extract_diff_header_lines(normalized_diff)
    rendered_segments: list[tuple[str, str]] = []
    pending_hunks: list[DiffHunk] = []

    for hunk in hunks:
        candidate_hunks = pending_hunks + [hunk]
        candidate_text = _render_hunk_group(header_lines, candidate_hunks)
        if pending_hunks and estimate_tokens(candidate_text) > segment_budget_tokens:
            rendered_segments.append(("hunk", _render_hunk_group(header_lines, pending_hunks)))
            pending_hunks = []

        single_hunk_text = _render_hunk_group(header_lines, [hunk])
        if estimate_tokens(single_hunk_text) > segment_budget_tokens:
            rendered_segments.extend(
                ("intra_hunk", chunk)
                for chunk in _split_single_hunk(
                    header_lines,
                    hunk,
                    segment_budget_tokens=segment_budget_tokens,
                )
            )
            continue

        pending_hunks.append(hunk)

    if pending_hunks:
        rendered_segments.append(("hunk", _render_hunk_group(header_lines, pending_hunks)))

    return _finalize_segments(normalized_path, rendered_segments, split_strategy=None)


def _build_batch(
    batch_index: int, segments: list[ReviewSegment], estimated_tokens: int
) -> ReviewBatch:
    paths = tuple(dict.fromkeys(segment.path for segment in segments))
    return ReviewBatch(
        batch_index=batch_index,
        estimated_tokens=estimated_tokens,
        segments=tuple(segments),
        paths=paths,
    )


def _finalize_segments(
    path: str,
    rendered_segments: list[tuple[str, str]],
    *,
    split_strategy: str | None,
) -> list[ReviewSegment]:
    total_segments = len(rendered_segments)
    finalized: list[ReviewSegment] = []
    for index, (strategy, text) in enumerate(rendered_segments):
        final_strategy = split_strategy or strategy
        finalized.append(
            ReviewSegment(
                path=path,
                diff_text=text,
                estimated_tokens=estimate_tokens(text),
                segment_index=index,
                total_segments=total_segments,
                split_strategy=final_strategy,
            )
        )
    return finalized


def _extract_diff_header_lines(diff_text: str) -> list[str]:
    lines: list[str] = []
    for line in diff_text.splitlines():
        if _HUNK_HEADER_RE.match(line):
            break
        lines.append(line)
    return lines


def _render_hunk_group(header_lines: Sequence[str], hunks: Sequence[DiffHunk]) -> str:
    lines = list(header_lines)
    for hunk in hunks:
        lines.append(
            _render_hunk_header(
                hunk.old_start,
                hunk.old_count,
                hunk.new_start,
                hunk.new_count,
            )
        )
        lines.extend(_render_hunk_lines(hunk.lines))
    return "\n".join(lines).strip()


def _split_single_hunk(
    header_lines: Sequence[str],
    hunk: DiffHunk,
    *,
    segment_budget_tokens: int,
) -> list[str]:
    out: list[str] = []
    start_index = 0
    while start_index < len(hunk.lines):
        end_index = start_index + 1
        best_end_index = start_index
        while end_index <= len(hunk.lines):
            candidate = _render_hunk_slice(header_lines, hunk, start_index, end_index)
            if estimate_tokens(candidate) > segment_budget_tokens:
                break
            best_end_index = end_index
            end_index += 1
        if best_end_index == start_index:
            out.extend(
                _split_oversized_hunk_line(
                    header_lines,
                    hunk,
                    start_index,
                    segment_budget_tokens=segment_budget_tokens,
                )
            )
            start_index += 1
            continue
        out.append(_render_hunk_slice(header_lines, hunk, start_index, best_end_index))
        start_index = best_end_index
    return out


def _split_oversized_hunk_line(
    header_lines: Sequence[str],
    hunk: DiffHunk,
    line_index: int,
    *,
    segment_budget_tokens: int,
) -> list[str]:
    content, old_ln, new_ln = hunk.lines[line_index]
    old_start, new_start = _slice_start_positions(hunk, line_index)
    old_count = 1 if old_ln is not None else 0
    new_count = 1 if new_ln is not None else 0

    def render_fragment(fragment: str) -> str:
        fragment_hunk = DiffHunk(
            path=hunk.path,
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            lines=[(fragment, old_ln, new_ln)],
        )
        return _render_hunk_slice(header_lines, fragment_hunk, 0, 1)

    fragments = _split_long_line(
        content,
        segment_budget_tokens,
        lambda fragment: estimate_tokens(render_fragment(fragment)),
    )
    return [render_fragment(fragment) for fragment in fragments]


def _render_hunk_slice(
    header_lines: Sequence[str],
    hunk: DiffHunk,
    start_index: int,
    end_index: int,
) -> str:
    old_start, new_start = _slice_start_positions(hunk, start_index)
    slice_lines = hunk.lines[start_index:end_index]
    old_count = sum(1 for _content, old_ln, _new_ln in slice_lines if old_ln is not None)
    new_count = sum(1 for _content, _old_ln, new_ln in slice_lines if new_ln is not None)
    lines = list(header_lines)
    lines.append(_render_hunk_header(old_start, old_count, new_start, new_count))
    lines.extend(_render_hunk_lines(slice_lines))
    return "\n".join(lines).strip()


def _slice_start_positions(hunk: DiffHunk, start_index: int) -> tuple[int, int]:
    old_cursor = hunk.old_start
    new_cursor = hunk.new_start
    for content, old_ln, new_ln in hunk.lines[:start_index]:
        if old_ln is not None:
            old_cursor = old_ln + 1
        if new_ln is not None:
            new_cursor = new_ln + 1
        if old_ln is None and new_ln is None and content.startswith(" No newline"):
            continue
    return (old_cursor, new_cursor)


def _render_hunk_header(old_start: int, old_count: int, new_start: int, new_count: int) -> str:
    return f"@@ -{old_start},{old_count} +{new_start},{new_count} @@"


def _render_hunk_lines(lines: Sequence[tuple[str, int | None, int | None]]) -> list[str]:
    rendered: list[str] = []
    for content, old_ln, new_ln in lines:
        if old_ln is not None and new_ln is not None:
            rendered.append(" " + content)
        elif new_ln is not None:
            rendered.append("+" + content)
        elif old_ln is not None:
            rendered.append("-" + content)
        else:
            rendered.append("\\" + content)
    return rendered


def _split_plain_text_segment(diff_text: str, segment_budget_tokens: int) -> list[tuple[str, str]]:
    lines = diff_text.splitlines()
    if not lines:
        return []
    out: list[tuple[str, str]] = []
    start_index = 0
    while start_index < len(lines):
        end_index = start_index + 1
        best_end_index = start_index
        while end_index <= len(lines):
            candidate = "\n".join(lines[start_index:end_index]).strip()
            if estimate_tokens(candidate) > segment_budget_tokens:
                break
            best_end_index = end_index
            end_index += 1
        if best_end_index == start_index:
            out.extend(
                ("line_fallback", fragment.strip())
                for fragment in _split_long_line(
                    lines[start_index],
                    segment_budget_tokens,
                    estimate_tokens,
                )
            )
            start_index += 1
            continue
        out.append(("line_fallback", "\n".join(lines[start_index:best_end_index]).strip()))
        start_index = best_end_index
    return out


def _split_long_line(
    line: str,
    segment_budget_tokens: int,
    estimate: Callable[[str], int],
) -> list[str]:
    if estimate(line) <= segment_budget_tokens or not line:
        return [line]

    fragments: list[str] = []
    remaining = line
    while remaining:
        low = 1
        high = len(remaining)
        best_length = 0
        while low <= high:
            mid = (low + high) // 2
            if estimate(remaining[:mid]) <= segment_budget_tokens:
                best_length = mid
                low = mid + 1
            else:
                high = mid - 1
        if best_length == 0:
            minimum_fragment_tokens = estimate(remaining[:1])
            if minimum_fragment_tokens > segment_budget_tokens:
                raise ValueError(
                    "Cannot split diff line within segment budget: "
                    f"minimum fragment requires {minimum_fragment_tokens} tokens "
                    f"but budget is {segment_budget_tokens}"
                )
            best_length = 1
        fragments.append(remaining[:best_length])
        remaining = remaining[best_length:]
    return fragments
