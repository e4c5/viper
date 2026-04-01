"""Prompt assembly helpers for the orchestration layer."""

from __future__ import annotations

def _supplement_char_budget(remaining_tokens: int | None) -> int | None:
    # Keep the same rough conversion as code_review.diff.utils.estimate_tokens().
    if remaining_tokens is None:
        return None
    return max(0, remaining_tokens * 4)


def _remaining_chars(max_chars: int | None, used_chars: int) -> int | None:
    if max_chars is None:
        return None
    return max_chars - used_chars


def _build_commit_messages_block(
    *,
    commit_messages: list[str],
    max_chars: int | None,
    already_used_chars: int,
) -> str:
    header = "### PR commit messages (subject / first line)\n"
    lines: list[str] = []
    local_used = len(header)
    for msg in commit_messages[:100]:
        remaining_for_line = _remaining_chars(max_chars, already_used_chars + local_used)
        if remaining_for_line is not None and remaining_for_line <= 6:
            break
        subject = (msg.splitlines()[0] if msg else "").strip()
        available = (remaining_for_line or 500) - 3
        subject_cap = min(500, max(0, available))
        line = f"- {subject[:subject_cap]}"
        lines.append(line)
        local_used += len(line) + 1
    return header + "\n".join(lines) if lines else ""


def _trim_context_brief(context_brief: str, remaining_chars: int | None) -> str:
    if remaining_chars is None:
        return context_brief
    if remaining_chars <= 0:
        return ""
    if len(context_brief) <= remaining_chars:
        return context_brief
    if remaining_chars <= 1:
        return ""
    return context_brief[: remaining_chars - 1] + "…"


def _format_review_prompt_supplement(
    *,
    context_brief: str | None,
    commit_messages: list[str],
    include_commit_messages: bool,
    remaining_tokens: int | None = None,
) -> str:
    """Extra user-message blocks: commit summaries and distilled external context."""
    max_chars = _supplement_char_budget(remaining_tokens)
    if max_chars == 0:
        return ""

    parts: list[str] = []
    used_chars = 0
    if include_commit_messages and commit_messages:
        commit_block = _build_commit_messages_block(
            commit_messages=commit_messages,
            max_chars=max_chars,
            already_used_chars=used_chars,
        )
        if commit_block:
            parts.append(commit_block)
            used_chars += len(commit_block)
    if context_brief:
        separator_chars = 2 if parts else 0
        remaining_for_context = _remaining_chars(max_chars, used_chars + separator_chars)
        trimmed_context = _trim_context_brief(context_brief, remaining_for_context)
        if trimmed_context:
            parts.append(trimmed_context)
    return "\n\n".join(parts) if parts else ""
