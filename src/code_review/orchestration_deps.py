"""Shared orchestration helpers for runner and ReviewOrchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from google.genai import types
from litellm import AuthenticationError

import code_review
from code_review import observability
from code_review.agent import create_review_agent, reply_dismissal_verdict_from_llm_text
from code_review.config import (
    get_code_review_app_config,
    get_context_aware_config,
    get_llm_config,
    get_scm_config,
)
from code_review.context import (
    ContextAwareFatalError,
    build_context_brief_for_pr,
    extract_context_references,
    validate_context_aware_sources,
)
from code_review.diff.fingerprint import (
    build_fingerprint,
    format_comment_body_with_marker,
    parse_marker_from_comment_body,
    surrounding_content_hash,
)
from code_review.diff.parser import (
    annotate_diff_with_line_numbers,
    iter_new_lines,
    parse_unified_diff,
)
from code_review.diff.position import get_diff_hunk_for_line
from code_review.formatters.comment import finding_to_comment_body, infer_severity_from_comment_body
from code_review.models import get_context_window
from code_review.providers import get_provider
from code_review.providers.base import (
    BotAttributionIdentity,
    InlineComment,
    RateLimitError,
    ReviewDecision,
    UnresolvedReviewItem,
    unified_diff_for_path,
)
from code_review.reply_dismissal_state import REPLY_DISMISSAL_ACCEPTED_REPLY_TEXT
from code_review.schemas.findings import FindingV1
from code_review.schemas.findings import FindingsBatchV1
from code_review.schemas.reply_dismissal import ReplyDismissalVerdictV1
from code_review.schemas.review_decision_event import (
    ReviewDecisionEventContext,
    event_allows_decision_only_skip_when_bot_not_blocking,
    review_decision_event_context_from_env,
)
from code_review.schemas.review_thread_dismissal import ReviewThreadDismissalContext
from code_review.standards import detect_from_paths, get_review_standards

APP_NAME = "code_review"
USER_ID = "reviewer"
AGENT_VERSION = getattr(code_review, "__version__", "0.1.0")
logger = logging.getLogger(__name__)

# Fraction of context window reserved for diff content; rest for system prompt, tools, response.
# Configurable via LLM_DIFF_BUDGET_RATIO env var.
try:
    DIFF_TOKEN_BUDGET_RATIO = float(os.getenv("LLM_DIFF_BUDGET_RATIO", "0.25"))
except ValueError:
    DIFF_TOKEN_BUDGET_RATIO = 0.25


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (chars / 4) for diff and context budget checks."""
    return max(0, len(text) // 4)


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


def _supplement_char_budget(remaining_tokens: int | None) -> int | None:
    # Keep the same rough conversion used by _estimate_tokens.
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
        subject_cap = min(500, max(40, (remaining_for_line or 500) - 4))
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


def _normalize_path_for_anchor(file_path: str) -> str:
    """Normalize path like Bitbucket provider for diff line matching.

    Strips ``dst://``, ``src://``, ``a/``, and ``b/`` prefixes.
    """
    p = (file_path or "").strip()
    for prefix in ("dst://", "src://"):
        if p.lower().startswith(prefix):
            p = p[len(prefix) :].lstrip("/")
            break
    p = p.lstrip("/")
    for prefix in ("a/", "b/"):
        if p.startswith(prefix):
            p = p[len(prefix) :]
            break
    return p.lstrip("/") or file_path or ""


def _added_lines_in_diff(diff_text: str) -> set[tuple[str, int]]:
    """Set of (normalized_path, line) for each added line in the diff (for line_type ADDED)."""
    out: set[tuple[str, int]] = set()
    for path, new_ln, _ in iter_new_lines(diff_text):
        out.add((_normalize_path_for_anchor(path), new_ln))
    return out


def _diff_visible_new_lines(diff_text: str) -> set[tuple[str, int]]:
    """Set of (normalized_path, new_line) for every line visible in the new-file diff.

    Includes both ADDED ('+' prefix) and CONTEXT (' ' prefix) lines — any line
    shown in the diff's new-file view that an SCM can anchor an inline comment to.
    Removed ('-' prefix) lines are excluded because they don't appear in the new-file view.

    Used as a guardrail: findings for lines outside this set cannot be placed inline
    and would appear only as PR-level activity comments on Bitbucket Cloud (and are
    rejected by GitHub/Gitea position-based APIs).
    """
    out: set[tuple[str, int]] = set()
    for hunk in parse_unified_diff(diff_text):
        norm_path = _normalize_path_for_anchor(hunk.path)
        for _content, _old_ln, new_ln in hunk.lines:
            if new_ln is not None:  # ADDED (old_ln=None) and CONTEXT (old_ln!=None)
                out.add((norm_path, new_ln))
    return out


def _build_diff_line_index(diff_text: str) -> dict[tuple[str, int], str]:
    """Build a mapping of (normalized_path, new_line) -> stripped line content from the diff.

    Only includes lines visible in the new-file view (ADDED '+' and CONTEXT ' ').
    Used by _validate_suggested_patches to check whether a patch is anchored to the
    correct line.
    """
    index: dict[tuple[str, int], str] = {}
    for hunk in parse_unified_diff(diff_text):
        norm_path = _normalize_path_for_anchor(hunk.path)
        for content, _old_ln, new_ln in hunk.lines:
            if new_ln is not None:
                index[(norm_path, new_ln)] = content.strip()
    return index


def _patch_tokens(text: str) -> set[str]:
    """Return a set of non-trivial tokens (len >= 3) from a code string, lower-cased.

    Used for a rough content-similarity check between a suggested_patch and the
    actual diff line it is anchored to.
    """
    return {tok.lower() for tok in re.split(r"\W+", text) if len(tok) >= 3}


def _normalize_code_for_comparison(text: str) -> str:
    """Remove whitespace outside quoted literals so formatting-only changes compare equal."""
    if not text:
        return ""

    out: list[str] = []
    quote_char: str | None = None
    escaped = False

    for ch in text:
        if quote_char is not None:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                quote_char = None
            continue

        if ch in {'"', "'", "`"}:
            quote_char = ch
            out.append(ch)
            continue

        if ch.isspace():
            continue

        out.append(ch)

    return "".join(out)


_SYNTAX_OR_MISSING_TOKEN_MESSAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bmissing\s+(?:a\s+)?(?:comma|semicolon|colon|parenthesis|paren|bracket|brace|quote)\b",
        re.I,
    ),
    re.compile(r"\bsyntax\s+error\b", re.I),
    re.compile(r"\b(?:invalid|malformed)\s+(?:\w+\s+)?code\b", re.I),
    re.compile(r"\b(?:invalid|malformed)\s+(?:annotation|statement|expression)\b", re.I),
    re.compile(r"\b(?:won['\u2019]?t|will\s+not)\s+compile\b", re.I),
    re.compile(r"\bcompiler?\s+error\b", re.I),
)

_QUOTE_DELIMITERS = frozenset({"`", "'", '"'})
_MISSING_COMMA_BEFORE_FRAGMENT_PATTERN = re.compile(
    r"\bmissing\s+(?:a\s+)?comma\s+before\s+(?:`([^`]+)`|\"([^\"]+)\"|'([^']+)')",
    re.I,
)


def _contains_word(text: str, word: str) -> bool:
    """Return True when word appears delimited by non-word characters."""
    start = 0
    while True:
        index = text.find(word, start)
        if index == -1:
            return False
        before_ok = index == 0 or not (text[index - 1].isalnum() or text[index - 1] == "_")
        after_index = index + len(word)
        after_ok = after_index == len(text) or not (
            text[after_index].isalnum() or text[after_index] == "_"
        )
        if before_ok and after_ok:
            return True
        start = index + len(word)


def _message_mentions_missing_quoted_fragment_before_or_after(message: str) -> bool:
    """Detect messages like 'missing `x` before ...' without broad backtracking regexes."""
    lowered = message.lower()
    search_from = 0
    while True:
        missing_index = lowered.find("missing", search_from)
        if missing_index == -1:
            return False

        fragment_start = missing_index + len("missing")
        while fragment_start < len(message) and message[fragment_start].isspace():
            fragment_start += 1

        if fragment_start >= len(message) or message[fragment_start] not in _QUOTE_DELIMITERS:
            search_from = missing_index + len("missing")
            continue

        delimiter = message[fragment_start]
        fragment_end = message.find(delimiter, fragment_start + 1)
        if fragment_end == -1:
            return False

        trailing_text = lowered[fragment_end + 1 :]
        if _contains_word(trailing_text, "before") or _contains_word(trailing_text, "after"):
            return True
        search_from = fragment_end + 1


def _message_describes_syntax_or_missing_token_issue(message: str) -> bool:
    """True when the finding text claims a token/syntax defect rather than a semantic issue."""
    if not message or not str(message).strip():
        return False
    return any(p.search(message) for p in _SYNTAX_OR_MISSING_TOKEN_MESSAGE_PATTERNS) or (
        _message_mentions_missing_quoted_fragment_before_or_after(message)
    )


def _extract_missing_comma_fragment(message: str) -> str | None:
    """Extract the cited fragment from messages like 'missing comma before `nullable = false`'."""
    if not message:
        return None
    match = _MISSING_COMMA_BEFORE_FRAGMENT_PATTERN.search(message)
    if not match:
        return None
    for group in match.groups():
        if group and group.strip():
            return group.strip()
    return None


def _window_text(lines_map: dict[int, str], line: int, radius: int = 2) -> str:
    """Join nearby visible diff lines into a small searchable context window."""
    window_lines = [
        content for ln, content in sorted(lines_map.items()) if line - radius <= ln <= line + radius
    ]
    return "\n".join(window_lines)


def _non_empty_patch_lines(suggested_patch: str) -> list[str]:
    """Return stripped non-empty lines from a suggested patch block."""
    return [ln.strip() for ln in suggested_patch.splitlines() if ln.strip()]


def _drop_or_strip_identical_patch_finding(
    finding: FindingV1,
    *,
    actual_content: str,
    message: str,
) -> FindingV1 | None:
    """Drop contradicted syntax findings or strip redundant suggestions."""
    if not finding.suggested_patch:
        return finding

    non_empty_lines = _non_empty_patch_lines(finding.suggested_patch)
    if len(non_empty_lines) != 1:
        return finding

    matches_current_line = _normalize_code_for_comparison(
        non_empty_lines[0]
    ) == _normalize_code_for_comparison(actual_content)
    if not matches_current_line:
        return finding

    if _message_describes_syntax_or_missing_token_issue(message):
        logger.info(
            "Dropping contradicted syntax/token finding %s:%d: "
            "suggested patch is identical to current diff line",
            finding.path,
            finding.line,
        )
        return None

    return finding.model_copy(update={"suggested_patch": None})


def _contradicted_missing_comma_fragment(message: str, window_text: str) -> str | None:
    """Return the contradicted fragment when nearby diff already contains `,<fragment>`."""
    fragment = _extract_missing_comma_fragment(message)
    if not fragment:
        return None

    fragment_patterns = (
        f", {fragment}",
        f",{fragment}",
    )
    if any(pattern in window_text for pattern in fragment_patterns):
        return fragment
    return None


def _filter_obviously_contradicted_findings(
    findings: list[FindingV1],
    diff_text: str,
) -> list[FindingV1]:
    """Drop findings whose own message is directly contradicted by visible diff code.

    This is intentionally conservative and only handles a few high-signal cases:
    - a syntax/token complaint whose suggested patch is identical to the current line
    - a message claiming a missing comma before a quoted fragment when the nearby diff
      already contains `,<fragment>`
    """
    if not diff_text or not findings:
        return findings

    line_index = _build_diff_line_index(diff_text)
    file_lines = _build_per_file_line_index(diff_text)
    kept: list[FindingV1] = []

    for f in findings:
        norm_path = _normalize_path_for_anchor(f.path)
        actual_content = line_index.get((norm_path, f.line))
        lines_map = file_lines.get(norm_path, {})
        if actual_content is None:
            kept.append(f)
            continue

        message = f.message or ""
        window_text = _window_text(lines_map, f.line)
        f = _drop_or_strip_identical_patch_finding(
            f,
            actual_content=actual_content,
            message=message,
        )
        if f is None:
            continue

        fragment = _contradicted_missing_comma_fragment(message, window_text)
        if fragment is not None:
            logger.info(
                "Dropping contradicted missing-comma finding %s:%d: "
                "nearby diff already contains comma before %r",
                f.path,
                f.line,
                fragment,
            )
            continue

        kept.append(f)

    return kept


def _validate_suggested_patches(
    findings: list[FindingV1],
    diff_text: str,
) -> list[FindingV1]:
    """Strip suggested_patch from findings where the patch doesn't match the anchored line.

    For each finding with a suggested_patch, look up the actual content of finding.line
    in the diff. If there is no meaningful token overlap between the patch's first line
    and the actual diff line, the patch is almost certainly misplaced (the LLM named a
    visible line but wrote a patch for a completely different piece of code).

    In that case, clear suggested_patch and log a warning so the finding is still posted
    as a plain comment rather than an incorrectly-placed suggestion block.

    Findings without a suggested_patch are returned unchanged.
    """
    if not diff_text or not findings:
        return findings

    line_index = _build_diff_line_index(diff_text)
    result: list[FindingV1] = []
    for f in findings:
        if not f.suggested_patch:
            result.append(f)
            continue

        norm_path = _normalize_path_for_anchor(f.path)
        actual_content = line_index.get((norm_path, f.line))
        if actual_content is None:
            # Line not in diff at all (will be caught by the visibility guardrail); keep as-is.
            result.append(f)
            continue

        # Use the first non-empty line of the patch for comparison (the replacement).
        patch_first_line = next(
            (ln.strip() for ln in f.suggested_patch.splitlines() if ln.strip()), ""
        )

        actual_tokens = _patch_tokens(actual_content)
        patch_tokens = _patch_tokens(patch_first_line)

        # If neither side has meaningful tokens, keep the patch as-is (degenerate case).
        if not actual_tokens or not patch_tokens:
            result.append(f)
            continue

        overlap = actual_tokens & patch_tokens
        # Require at least one shared token OR the actual line is very short (could be
        # a single symbol like '{' or a blank line that the LLM replaces entirely).
        is_plausible = bool(overlap) or len(actual_content) <= 5

        if not is_plausible:
            logger.warning(
                "Stripping misplaced suggested_patch from finding %s:%d: "
                "patch first line %r has no token overlap with actual diff line %r",
                f.path,
                f.line,
                patch_first_line,
                actual_content,
            )
            # Build a new FindingV1 without the patch.  FindingV1 is a Pydantic model;
            # model_copy(update=...) is the safe, idiomatic way to produce a mutated copy.
            f = f.model_copy(update={"suggested_patch": None})

        result.append(f)
    return result


# Model sometimes emits stream-of-consciousness findings then retracts them in the same message.
# Patterns tie the walk-back to the model / this finding (I, this, that, it), not domain jargon.
_SELF_RETRACTION_MESSAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:i\s+will|i['\u2019]ll)\s+retract\b", re.I),
    re.compile(r"\bi\s+retract\b", re.I),
    re.compile(r"\bretract(?:ed|ing)?\s+this\s+finding\b", re.I),
    re.compile(r"\bretract\s+this\b", re.I),
    re.compile(r"\b(?:this|that|it)\s+is\s+(?:a\s+)?false\s+positive\b", re.I),
    re.compile(r"\bthat\s+was\s+(?:a\s+)?false\s+positive\b", re.I),
    re.compile(r"\bit's\s+(?:a\s+)?false\s+positive\b", re.I),
    re.compile(r"\bwithdraw\s+this\s+finding\b", re.I),
    re.compile(r"\bdisregard\s+this\b", re.I),
    re.compile(r"\bignore\s+this\s+(?:finding|comment)\b", re.I),
    re.compile(r"\b(?:i|we)\s+no\s+longer\s+(?:believe|think)\b", re.I),
    re.compile(r"\bi\s+was\s+wrong\b", re.I),
    re.compile(r"\bi\s+was\s+mistaken\b", re.I),
    re.compile(r"\bmy\s+mistake\b", re.I),
    re.compile(r"\bon\s+second\s+thought\b", re.I),
    re.compile(r"\bupon\s+reflection\b", re.I),
    re.compile(r"\b(?:sorry|apologies),?\s+(?:ignore|disregard)\b", re.I),
    re.compile(r"\bactually,?\s+this\s+is\s+(?:fine|correct|acceptable)\b", re.I),
    re.compile(r"\bhowever,?\s+this\s+is\s+(?:fine|correct|acceptable)\b", re.I),
)


def _finding_message_looks_self_retracted(message: str) -> bool:
    """True when the model walked back or disowned the issue inside the same message."""
    if not message or not str(message).strip():
        return False
    return any(p.search(message) for p in _SELF_RETRACTION_MESSAGE_PATTERNS)


def _filter_self_retracted_finding_messages(findings: list[FindingV1]) -> list[FindingV1]:
    """Drop findings whose message text retracts or negates the issue (non-actionable noise)."""
    if not findings:
        return findings
    kept: list[FindingV1] = []
    for f in findings:
        if _finding_message_looks_self_retracted(f.message):
            logger.info(
                "Dropping finding with self-retracted or withdrawn message text: %s:%d",
                f.path,
                f.line,
            )
            continue
        kept.append(f)
    return kept


# Default search radius for anchor-based line relocation (lines above & below finding.line).
_ANCHOR_RELOCATION_WINDOW = 20


def _build_per_file_line_index(
    diff_text: str,
) -> dict[str, dict[int, str]]:
    """Build {normalized_path: {new_line_no: stripped_content}} from a unified diff.

    Only includes lines visible in the new-file view (ADDED and CONTEXT).
    """
    file_lines: dict[str, dict[int, str]] = {}
    for hunk in parse_unified_diff(diff_text):
        norm_path = _normalize_path_for_anchor(hunk.path)
        bucket = file_lines.setdefault(norm_path, {})
        for content, _old_ln, new_ln in hunk.lines:
            if new_ln is not None:
                bucket[new_ln] = content.strip()
    return file_lines


def _find_closest_anchor_line(
    lines_map: dict[int, str],
    anchor_text: str,
    reported_line: int,
    window: int,
) -> int | None:
    """Return the closest line number whose content contains *anchor_text* (case-insensitive).

    Only considers lines within *window* of *reported_line*.  Returns ``None``
    when no match is found or the best match is the reported line itself.
    """
    anchor_lower = anchor_text.lower()
    best_line: int | None = None
    best_distance = window + 1
    for ln, content in lines_map.items():
        if anchor_lower not in content.lower():
            continue
        distance = abs(ln - reported_line)
        if distance <= window and distance < best_distance:
            best_line = ln
            best_distance = distance
    return best_line


def _relocate_findings_by_anchor(
    findings: list[FindingV1],
    diff_text: str,
    window: int = _ANCHOR_RELOCATION_WINDOW,
) -> list[FindingV1]:
    """Correct finding line numbers when the anchor text doesn't match the reported line.

    The LLM sometimes identifies the right code issue but reports a line number
    that is off by a few lines.  When a finding has a non-empty ``anchor`` or
    ``fingerprint_hint``, this function checks whether that text appears in the
    diff content at ``finding.line``.  If not, it searches nearby visible lines
    (within *window* lines above and below) in the same file and relocates the
    finding to the closest line whose content contains the anchor substring.

    Findings without an anchor/fingerprint_hint, or whose anchor already matches
    at the reported line, are returned unchanged.
    """
    if not diff_text or not findings:
        return findings

    file_lines = _build_per_file_line_index(diff_text)

    result: list[FindingV1] = []
    for f in findings:
        result.append(_maybe_relocate_finding(f, file_lines, window))
    return result


def _maybe_relocate_finding(
    f: FindingV1,
    file_lines: dict[str, dict[int, str]],
    window: int,
) -> FindingV1:
    """Return *f* relocated to the correct line if anchor text doesn't match, else unchanged."""
    anchor_text = (f.anchor or f.fingerprint_hint or "").strip()
    if not anchor_text:
        return f

    norm_path = _normalize_path_for_anchor(f.path)
    lines_map = file_lines.get(norm_path)
    if not lines_map:
        return f

    # Anchor already present at the reported line — nothing to do.
    current_content = lines_map.get(f.line, "")
    if anchor_text.lower() in current_content.lower():
        return f

    best_line = _find_closest_anchor_line(lines_map, anchor_text, f.line, window)
    if best_line is not None and best_line != f.line:
        logger.info(
            "Relocating finding %s:%d -> %d (anchor %r found at line %d)",
            f.path,
            f.line,
            best_line,
            anchor_text,
            best_line,
        )
        return f.model_copy(update={"line": best_line})
    return f


def _build_idempotency_key(
    scm_cfg,
    llm_cfg,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    base_sha: str = "",
) -> str:
    """Idempotency key: same key => same run already done for this PR/range/config."""
    head_sha = (head_sha or "").strip()
    base_sha = (base_sha or "").strip()
    config_hash = hashlib.sha256(
        f"{scm_cfg.provider}:{scm_cfg.url}:{llm_cfg.provider}:{llm_cfg.model}".encode()
    ).hexdigest()[:16]
    return (
        f"{scm_cfg.provider}/{owner}/{repo}/pr/{pr_number}/head/{head_sha}/base/{base_sha}/"
        f"agent/{AGENT_VERSION}/config/{config_hash}"
    )


def _idempotency_key_seen_in_comments(comments: list, key: str) -> bool:
    """Return True if any comment body contains run=<key> in code-review-agent marker."""
    for c in comments:
        body = getattr(c, "body", None) or (c.get("body") if isinstance(c, dict) else "")
        if body:
            parsed = parse_marker_from_comment_body(body)
            if parsed.get("run") == key:
                return True
    return False


def _should_skip_finding_for_dedup(
    path: str,
    body_hash: str,
    fp: str,
    ignore_set: set[tuple[str, str]],
    resolved_body_set: set[tuple[str, str]],
    resolved_fp_set: set[tuple[str, str]],
) -> bool:
    """Return True if this finding should be skipped (duplicate or resolved)."""
    if fp and (path, fp) in resolved_fp_set:
        return True
    if (path, body_hash) in ignore_set and (path, body_hash) not in resolved_body_set:
        return True
    if fp and (path, fp) in ignore_set and (path, fp) not in resolved_fp_set:
        return True
    return False


def _build_ignore_set(comments: list) -> set[tuple[str, str]]:
    """
    Build set of (path, key) from existing review comments.
    Key is fingerprint (from marker) or body_hash for dedup and manually-resolved ignore.
    """
    out: set[tuple[str, str]] = set()
    for c in comments:
        path = getattr(c, "path", None) or (c.get("path") if isinstance(c, dict) else "")
        body = getattr(c, "body", None) or (c.get("body") if isinstance(c, dict) else "")
        if not path or not body:
            continue
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        out.add((path, body_hash))
        parsed = parse_marker_from_comment_body(body)
        if parsed.get("fingerprint"):
            out.add((path, parsed["fingerprint"]))
    return out


def _get_file_lines_by_path(
    provider, owner: str, repo: str, ref: str, paths: list[str]
) -> dict[str, list[str]]:
    """Fetch file content at ref for each path; return dict path -> list of lines."""
    out: dict[str, list[str]] = {}
    for p in paths:
        try:
            content = provider.get_file_content(owner, repo, ref, p)
            out[p] = content.splitlines()
        except Exception as e:
            logger.warning(
                "get_file_content failed for path=%s owner=%s repo=%s ref=%s: %s",
                p,
                owner,
                repo,
                ref,
                e,
                exc_info=True,
            )
            out[p] = []
    return out


def _generate_auto_pr_description(title: str, paths: list[str], max_files: int = 10) -> str:
    """
    Build a non-empty, deterministic PR description when the user did not add one.

    Uses the title and the list of changed file paths so the PR has a useful
    description. Guarantees a non-empty string so it is safe to set as the PR body.
    """
    title_str = title.strip() or "Untitled change"
    unique_paths = list(dict.fromkeys(paths))
    shown_paths = unique_paths[:max_files]
    files_part = ", ".join(f"`{p}`" for p in shown_paths) if shown_paths else "no files detected"
    more_suffix = ""
    if len(unique_paths) > max_files:
        more_suffix = f", and {len(unique_paths) - max_files} more file(s)"
    out = (
        f"**Title**: {title_str}\n\n"
        f"This pull request updates {len(unique_paths)} file(s): {files_part}{more_suffix}."
    )
    return out.strip() or "Auto-generated summary."


def _maybe_post_started_review_comment(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    pr_info,
    paths: list[str],
) -> None:
    """
    When the user has not added a description to the PR: generate a non-empty
    description, update the PR with it when the SCM API allows, then post
    general notes about the PR and the review.
    """
    if not pr_info:
        return
    if not paths:
        return
    description = (getattr(pr_info, "description", "") or "").strip()
    # Treat very short descriptions as effectively missing.
    if len(description) >= 40:
        return
    generated = _generate_auto_pr_description(getattr(pr_info, "title", "") or "", paths)
    if not generated or not generated.strip():
        return
    # 1) Update the PR description when the SCM API allows it.
    description_updated = False
    try:
        provider.update_pr_description(owner, repo, pr_number, generated)
        description_updated = True
    except NotImplementedError:
        pass
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "update_pr_description failed owner=%s repo=%s pr_number=%s: %s",
            owner,
            repo,
            pr_number,
            e,
        )
    # 2) Post general notes about the PR and the review.
    if description_updated:
        notes = (
            "Viper has started a review of this pull request and updated the "
            "PR description with an auto-generated summary."
        )
    else:
        notes = (
            "Viper has started a review of this pull request.\n\n"
            "The PR had no description and this SCM does not support updating it; "
            "below is the summary we generated for context:\n\n"
            f"{generated}"
        )
    try:
        provider.post_pr_summary_comment(owner, repo, pr_number, notes)
    except Exception as e:  # pragma: no cover - defensive; providers should implement this
        logger.warning(
            "post_pr_summary_comment (started review) failed owner=%s repo=%s pr_number=%s: %s",
            owner,
            repo,
            pr_number,
            e,
        )


def _resolve_stale_comments_if_supported(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    existing: list,
    to_post: list[tuple[FindingV1, str]],
    head_sha: str,
    dry_run: bool,
) -> None:
    """If provider supports it, resolve comments whose fingerprint is no longer in to_post."""
    if not (provider.capabilities().resolvable_comments and head_sha and not dry_run):
        return
    new_fps = {fp for _, fp in to_post if fp}
    for c in existing:
        body = getattr(c, "body", "") or ""
        parsed = parse_marker_from_comment_body(body)
        fp_old = parsed.get("fingerprint")
        if not fp_old or fp_old in new_fps:
            continue
        try:
            provider.resolve_comment(owner, repo, c.id)
        except Exception as e:
            logger.warning(
                "resolve_comment failed owner=%s repo=%s pr_number=%s comment_id=%s: %s",
                owner,
                repo,
                pr_number,
                getattr(c, "id", ""),
                e,
            )


def _post_inline_comments(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    incremental_base_sha: str,
    to_post: list[tuple[FindingV1, str]],
    cfg,
    llm_cfg,
    full_diff: str = "",
) -> int:
    """Build inline comments and post each one individually. Returns successful post count."""
    caps = provider.capabilities()
    run_id = _build_idempotency_key(
        cfg, llm_cfg, owner, repo, pr_number, head_sha, incremental_base_sha
    )
    added_set = _added_lines_in_diff(full_diff) if full_diff else set()
    comments: list[InlineComment] = []
    for f, fp in to_post:
        body = finding_to_comment_body(f, use_collapsible_prompt=caps.markup_supports_collapsible)
        if fp and not caps.omit_fingerprint_marker_in_body:
            body = format_comment_body_with_marker(
                body,
                fp,
                AGENT_VERSION,
                run_id=run_id,
                marker_at_end=not caps.markup_hides_html_comment,
            )
        line_type: str | None = None
        if added_set:
            norm_path = _normalize_path_for_anchor(f.path)
            line_type = "ADDED" if (norm_path, f.line) in added_set else "CONTEXT"
        comments.append(
            InlineComment(
                path=f.path,
                line=f.line,
                body=body,
                end_line=f.end_line,
                suggested_patch=f.suggested_patch,
                line_type=line_type,
            )
        )
    return _post_comments_one_by_one(provider, owner, repo, pr_number, head_sha, comments)


def _post_comments_one_by_one(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    comments: list[InlineComment],
) -> int:
    """Post each comment individually; skip (warn) on failure. Returns successful count.

    No fallback to PR summary comments: mirrors the tool-based (file-by-file) behaviour
    where a failed inline comment is simply skipped and the next one is attempted.
    """
    count = 0
    for c in comments:
        try:
            # Use post_review_comments([c]) rather than post_review_comment() so that
            # provider-specific fields on the InlineComment (e.g. line_type, used by
            # Bitbucket Server for lineType="ADDED"|"CONTEXT") are preserved.
            # post_review_comment() in the base class reconstructs InlineComment without
            # these fields, causing Bitbucket Server to default to lineType="ADDED" for
            # every line — which results in HTTP 409 for context (unchanged) lines.
            provider.post_review_comments(
                owner,
                repo,
                pr_number,
                [c],
                head_sha=head_sha,
            )
            count += 1
        except Exception as e:
            logger.warning(
                "post_review_comment failed owner=%s repo=%s pr_number=%s path=%s line=%s: %s",
                owner,
                repo,
                pr_number,
                c.path,
                c.line,
                e,
            )
    return count


def _omit_marker_pr_summary_visible_text(
    *,
    findings_planned: int,
    successful_inline_posts: int,
    cfg,
    provider,
    gate_outcome: QualityGateReviewOutcome,
) -> str:
    """Human-readable PR summary for providers that omit inline HTML markers (e.g. Bitbucket)."""
    lines: list[str] = [
        "**Viper** (automated code review) finished for this pull request at the current revision."
    ]
    if findings_planned == 0:
        lines.append(
            "It **did not flag new issues** that require inline comments in this run "
            "(within the reviewed diff scope and your ignore rules)."
        )
        gate_in_summary = bool(getattr(cfg, "review_decision_enabled", False)) and (
            provider.capabilities().supports_review_decisions
        )
        if not gate_in_summary or gate_outcome.decision != "REQUEST_CHANGES":
            lines.append(
                "**From this automated pass, the change appears to meet expectations** "
                "for the areas reviewed."
            )
    else:
        lines.append(f"It **identified {findings_planned} issue(s)** worth addressing on the diff.")
        if successful_inline_posts >= findings_planned:
            lines.append(f"**Posted {successful_inline_posts} inline comment(s)** on the diff.")
        elif successful_inline_posts > 0:
            lines.append(
                f"**Posted {successful_inline_posts} of {findings_planned} inline comment(s)**; "
                "some could not be anchored (see CI logs)."
            )
        else:
            lines.append(
                "**Could not post inline comments** (e.g. anchor conflicts); see CI logs. "
                "Re-run after updating the PR or fixing the reported problems."
            )

    extra = _optional_quality_gate_summary_suffix(provider, cfg, gate_outcome)
    if extra:
        lines.append(extra)
    return "\n\n".join(lines)


def _optional_quality_gate_summary_suffix(
    provider,
    cfg,
    gate_outcome: QualityGateReviewOutcome,
) -> str:
    """Append threshold / merge-gate wording when review decisions are enabled.

    *gate_outcome* must be the same snapshot used for :func:`_maybe_submit_review_decision`.
    """
    if not bool(getattr(cfg, "review_decision_enabled", False)):
        return ""
    if not provider.capabilities().supports_review_decisions:
        return ""
    high_threshold = int(getattr(cfg, "review_decision_high_threshold", 1))
    medium_threshold = int(getattr(cfg, "review_decision_medium_threshold", 3))
    if gate_outcome.decision == "REQUEST_CHANGES":
        return (
            f"Given your configured thresholds, Viper **suggests this PR needs work** before merge "
            f"(open high={gate_outcome.high_count} vs threshold {high_threshold}, "
            f"open medium={gate_outcome.medium_count} vs threshold {medium_threshold})."
        )
    return (
        f"Given your configured thresholds, this PR **passes Viper's automated merge gate** "
        f"(open high={gate_outcome.high_count}, open medium={gate_outcome.medium_count})."
    )


def _post_omit_marker_pr_summary_comment(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    cfg,
    llm_cfg,
    head_sha: str,
    incremental_base_sha: str = "",
    *,
    findings_planned: int,
    successful_inline_posts: int,
    gate_outcome: QualityGateReviewOutcome,
    include_run_marker: bool = True,
) -> None:
    """Post a PR-level summary for omit-marker providers; optionally attach the ``run=`` id marker.

    The run marker must only be included when every planned inline comment was posted (or
    there were none to post). Otherwise a later CI run would short-circuit on idempotency
    and never retry failed inline posts.

    When *include_run_marker* is False, only the visible summary is posted (still useful
    for operators); no idempotency short-circuit until a fully successful post run.
    """
    caps = provider.capabilities()
    visible = _omit_marker_pr_summary_visible_text(
        findings_planned=findings_planned,
        successful_inline_posts=successful_inline_posts,
        cfg=cfg,
        provider=provider,
        gate_outcome=gate_outcome,
    )
    if include_run_marker:
        run_id = _build_idempotency_key(
            cfg, llm_cfg, owner, repo, pr_number, head_sha, incremental_base_sha
        )
        use_linkref = getattr(caps, "embed_agent_marker_as_commonmark_linkref", None) is True
        body = format_comment_body_with_marker(
            visible,
            "",
            AGENT_VERSION,
            run_id=run_id,
            marker_at_end=not caps.markup_hides_html_comment,
            use_commonmark_linkref=use_linkref,
        )
    else:
        body = visible
    try:
        provider.post_pr_summary_comment(owner, repo, pr_number, body)
    except Exception as e:
        logger.warning(
            "_post_omit_marker_pr_summary_comment failed owner=%s repo=%s pr_number=%s: %s",
            owner,
            repo,
            pr_number,
            e,
        )


def _quality_gate_dedupe_key_for_item(item: UnresolvedReviewItem) -> str:
    """Stable key so the same issue is not double-counted (marker fingerprint preferred)."""
    parsed = parse_marker_from_comment_body(item.body)
    fp = parsed.get("fingerprint")
    if fp:
        return f"fp:{fp}"
    if item.thread_id:
        return f"thread:{item.thread_id}"
    return f"id:{item.stable_id}"


def _quality_gate_dedupe_key_for_new_finding(finding: FindingV1, fp: str) -> str:
    if fp:
        return f"fp:{fp}"
    return f"new:{finding.path}:{finding.line}:{finding.code}"


def _quality_gate_fetch_unresolved_items(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
) -> list[Any]:
    try:
        items = provider.get_unresolved_review_items_for_quality_gate(owner, repo, pr_number)
    except Exception as e:
        logger.warning(
            "get_unresolved_review_items_for_quality_gate failed owner=%s repo=%s pr_number=%s: %s",
            owner,
            repo,
            pr_number,
            e,
        )
        return []
    return items if isinstance(items, list) else []


def _quality_gate_bump_seen(
    seen_keys: set[str],
    high_count: int,
    medium_count: int,
    key: str,
    severity: str,
) -> tuple[int, int]:
    if key in seen_keys:
        return high_count, medium_count
    seen_keys.add(key)
    if severity == "high":
        return high_count + 1, medium_count
    if severity == "medium":
        return high_count, medium_count + 1
    return high_count, medium_count


def _quality_gate_high_medium_counts(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    to_post: list[tuple[FindingV1, str]],
    *,
    excluded_stable_ids: frozenset[str] | None = None,
) -> tuple[int, int]:
    """Count distinct open high/medium signals: existing unresolved items plus net-new findings."""
    items = _quality_gate_fetch_unresolved_items(provider, owner, repo, pr_number)
    skip_ids = excluded_stable_ids or frozenset()
    seen_keys: set[str] = set()
    high_count = 0
    medium_count = 0

    for raw in items:
        if not isinstance(raw, UnresolvedReviewItem):
            continue
        if raw.stable_id in skip_ids:
            continue
        sev = raw.inferred_severity
        if sev not in ("high", "medium"):
            continue
        high_count, medium_count = _quality_gate_bump_seen(
            seen_keys, high_count, medium_count, _quality_gate_dedupe_key_for_item(raw), sev
        )

    for finding, fp in to_post:
        sev = finding.severity
        if sev not in ("high", "medium"):
            continue
        high_count, medium_count = _quality_gate_bump_seen(
            seen_keys,
            high_count,
            medium_count,
            _quality_gate_dedupe_key_for_new_finding(finding, fp),
            sev,
        )

    return high_count, medium_count


def _compute_review_decision_from_counts(
    high_count: int,
    medium_count: int,
    *,
    high_threshold: int,
    medium_threshold: int,
) -> ReviewDecision:
    """Return REQUEST_CHANGES or APPROVE from aggregated open high/medium counts."""
    if high_count >= high_threshold or medium_count >= medium_threshold:
        return "REQUEST_CHANGES"
    return "APPROVE"


@dataclass(frozen=True)
class QualityGateReviewOutcome:
    """Aggregated quality-gate counts and derived review decision (single source of truth)."""

    high_count: int
    medium_count: int
    decision: ReviewDecision
    submission_reason: str


def _log_quality_gate_review_outcome(context: str, gate_outcome: QualityGateReviewOutcome) -> None:
    """Emit a stable log line for the computed quality gate and derived PR decision."""
    logger.info(
        "%s quality gate: open_high=%d open_medium=%d => decision=%s",
        context,
        gate_outcome.high_count,
        gate_outcome.medium_count,
        gate_outcome.decision,
    )


def _compute_quality_gate_review_outcome(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    to_post: list[tuple[FindingV1, str]],
    cfg,
    *,
    excluded_gate_stable_ids: frozenset[str] | None = None,
) -> QualityGateReviewOutcome:
    """Combine provider unresolved items with planned posts; apply thresholds; build reason text."""
    high_count, medium_count = _quality_gate_high_medium_counts(
        provider,
        owner,
        repo,
        pr_number,
        to_post,
        excluded_stable_ids=excluded_gate_stable_ids,
    )
    high_threshold = int(getattr(cfg, "review_decision_high_threshold", 1))
    medium_threshold = int(getattr(cfg, "review_decision_medium_threshold", 3))
    decision = _compute_review_decision_from_counts(
        high_count,
        medium_count,
        high_threshold=high_threshold,
        medium_threshold=medium_threshold,
    )
    submission_reason = (
        f"Auto decision by Viper: aggregated open high={high_count} (threshold {high_threshold}), "
        f"open medium={medium_count} (threshold {medium_threshold}) "
        f"=> {decision}."
    )
    return QualityGateReviewOutcome(
        high_count=high_count,
        medium_count=medium_count,
        decision=decision,
        submission_reason=submission_reason,
    )


def _resolve_head_sha_for_review_decision_submission(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
) -> str:
    """Use caller-provided head_sha, or fetch current PR head from the provider when empty."""
    stripped = (head_sha or "").strip()
    if stripped:
        return stripped
    try:
        pr_info = provider.get_pr_info(owner, repo, pr_number)
    except Exception as e:
        logger.warning(
            "get_pr_info failed while resolving head_sha owner=%s repo=%s pr_number=%s: %s",
            owner,
            repo,
            pr_number,
            e,
        )
        return ""
    if pr_info is None:
        return ""
    resolved = (getattr(pr_info, "head_sha", None) or "").strip()
    if resolved:
        logger.info(
            "Resolved PR head_sha from provider for review decision (prefix=%s)",
            resolved[:12],
        )
    return resolved


def _log_review_decision_event_if_present(ctx: ReviewDecisionEventContext | None) -> None:
    """Emit one info line when webhook/CI event metadata is present (decision-only runs)."""
    if ctx is None or not ctx.has_audit_fields():
        return
    logger.info(
        "review_decision_event_context source=%s comment_id=%s thread_id=%s actor_login=%s",
        ctx.source,
        ctx.comment_id,
        ctx.thread_id,
        ctx.actor_login,
    )


def _reply_dismissal_response_log_snippet(text: str, limit: int = 1000) -> str:
    """Return a bounded single-string snippet for reply-dismissal logs."""
    snippet = (text or "").strip()
    if len(snippet) > limit:
        snippet = snippet[:limit] + "…"
    return snippet or "(empty)"


def _head_sha_hint_for_decision_only(
    cli_head_sha: str,
) -> str:
    """Return CLI/env head SHA for decision-only runs."""
    return (cli_head_sha or "").strip()


def _maybe_submit_review_decision(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    dry_run: bool,
    cfg,
    *,
    gate_outcome: QualityGateReviewOutcome,
) -> None:
    """Submit or log PR-level review decision when configured and supported.

    *gate_outcome* must match the omit-marker PR summary snapshot when both run in the same pass.
    """
    if not bool(getattr(cfg, "review_decision_enabled", False)):
        logger.info(
            "Skipping PR review decision submission: SCM_REVIEW_DECISION_ENABLED is false "
            "(enable to push the computed gate to the SCM).",
        )
        return

    decision = gate_outcome.decision
    reason = gate_outcome.submission_reason

    caps = provider.capabilities()
    if not caps.supports_review_decisions:
        logger.info(
            "Skipping review decision submission: provider does not support review decisions "
            "(would submit %s).",
            decision,
        )
        return

    if dry_run:
        logger.info("Dry run: would submit PR review decision=%s", decision)
        return

    try:
        provider.submit_review_decision(
            owner,
            repo,
            pr_number,
            decision,
            body=reason,
            head_sha=head_sha,
        )
    except Exception as e:
        logger.warning(
            "Failed to submit PR review decision=%s owner=%s repo=%s pr=%s: %s",
            decision,
            owner,
            repo,
            pr_number,
            e,
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        return
    logger.info("Submitted PR review decision=%s", decision)


def _fingerprint_for_finding(
    f: FindingV1,
    file_lines_by_path: dict[str, list[str]],
    window: int = 2,
) -> str:
    """Compute stable fingerprint for a finding (path, content_hash, issue_code, anchor)."""
    lines = file_lines_by_path.get(f.path, [])
    content_hash_val = surrounding_content_hash(lines, f.line, window)
    anchor = (f.anchor or f.fingerprint_hint or "").strip()
    return build_fingerprint(f.path, content_hash_val, f.code, anchor or None)


def _parse_findings_json(text: str) -> object:
    """Parse findings JSON from a raw body or a fenced JSON block.

    Preferred shape is a top-level object like ``{"findings": [...]}``.
    For backward compatibility, a bare JSON array of findings is also accepted.
    """
    text = text.strip()
    candidates = []
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text)
    seen: set[str] = set()
    for raw in candidates:
        if not raw or raw in seen:
            continue
        seen.add(raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    return []


def _normalize_finding_item(item: dict) -> dict:
    """Normalize compatible finding payload fields before model validation."""
    if "anchor" in item and "fingerprint_hint" not in item:
        item = {**item, "fingerprint_hint": item.get("anchor")}
    return item


def _findings_from_response(response_text: str) -> list[FindingV1]:
    """Parse response text into validated findings.

    Preferred format is ``{"findings": [...]}``. A bare array is still accepted
    as a backward-compatibility path while tests and prompts migrate.
    """
    raw = _parse_findings_json(response_text)

    if isinstance(raw, dict) and isinstance(raw.get("findings"), list):
        try:
            normalized = {
                **raw,
                "findings": [
                    _normalize_finding_item(item)
                    for item in raw["findings"]
                    if isinstance(item, dict)
                ],
            }
            return FindingsBatchV1.model_validate(normalized).findings
        except Exception as e:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Failed to parse structured findings response: %r (error: %s)",
                    raw,
                    e,
                    exc_info=True,
                )
            return []

    # Backward compatibility for the pre-output_schema contract.
    if isinstance(raw, dict):
        raw = [raw]

    findings: list[FindingV1] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            findings.append(FindingV1.model_validate(_normalize_finding_item(item)))
        except Exception as e:
            # Be tolerant of partial/malformed items, but surface details at DEBUG level
            # so it's clear we're skipping something the model tried to return.
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Skipping invalid finding item from LLM response: %r (error: %s)",
                    item,
                    e,
                    exc_info=True,
                )
            continue
    return findings


def _log_run_complete(
    trace_id: str,
    owner: str,
    repo: str,
    pr_number: int,
    files_count: int,
    findings_count: int,
    posts_count: int,
    duration_ms: float,
) -> None:
    """Emit structured run_complete log (Phase 4.3)."""
    logger.info(
        "run_complete",
        extra={
            "trace_id": trace_id,
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "files_count": files_count,
            "findings_count": findings_count,
            "posts_count": posts_count,
            "duration_ms": round(duration_ms, 2),
        },
    )


def _suppress_ssl_teardown_errors(loop, context: dict) -> None:
    """Asyncio exception handler that silences known SSL-transport teardown noise.

    When asyncio.run() closes the event loop the Google GenAI SDK's HTTPS
    connections may still be flushing their SSL write-backlog.  The underlying
    socket file-descriptor has already been closed, so the write raises
    ``OSError: [Errno 9] Bad file descriptor``, which asyncio turns into a
    ``RuntimeError: Event loop is closed``.  Neither of these is actionable —
    the review completed successfully — so we drop them here and fall through
    to the default handler for everything else.
    """
    exc = context.get("exception")
    msg = context.get("message", "")
    _teardown_msg = "SSL" in msg or "Fatal write error" in msg or "write backlog" in msg
    _teardown_exc = (isinstance(exc, OSError) and getattr(exc, "errno", None) == 9) or (
        isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc)
    )
    if _teardown_msg and _teardown_exc:
        return
    loop.default_exception_handler(context)


async def _collect_response_async(
    runner, session_service, session_id: str, content: types.Content
) -> str:
    """Run agent once via run_async and return concatenated final response text.

    When CODE_REVIEW_LOG_LEVEL=DEBUG, log the raw final text we received from the LLM.
    """
    # Install the exception handler early so it covers SSL teardown that occurs
    # when asyncio.run() calls loop.close() after this coroutine returns.
    asyncio.get_running_loop().set_exception_handler(_suppress_ssl_teardown_errors)

    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
    )
    parts: list[str] = []
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                # Only use text parts; skip function_call etc. to avoid SDK warning
                if getattr(part, "text", None):
                    parts.append(part.text)
    text = "\n".join(parts)
    if logger.isEnabledFor(logging.DEBUG):
        # Log raw agent output for debugging schema/JSON issues.
        logger.debug("LLM final response for session %s:\n%s", session_id, text)
    # Optional: also echo raw response to stdout when explicitly requested.
    # This is controlled by CODE_REVIEW_PRINT_RAW_RESPONSE=1 for ad-hoc debugging.
    if os.getenv("CODE_REVIEW_PRINT_RAW_RESPONSE", "").strip() in ("1", "true", "TRUE"):
        print(f"RAW LLM RESPONSE (session={session_id}):\n{text}")
    return text


def _run_agent_and_collect_response(
    runner, session_service, session_id: str, content: types.Content
) -> str:
    """Run agent once and return concatenated final response text (uses async API)."""
    return asyncio.run(_collect_response_async(runner, session_service, session_id, content))


def _normalize_scm_identity_fragment(value: str) -> str:
    """Lowercase and strip braces/spaces for comparing SCM user/uuid strings."""
    return (value or "").strip().lower().replace("{", "").replace("}", "")


def _event_actor_matches_bot_login(actor_login: str, bot: BotAttributionIdentity) -> bool:
    bot_login = (bot.login or "").strip()
    return bool(bot_login and actor_login and actor_login.lower() == bot_login.lower())


def _event_actor_matches_bot_id(actor_id: str, bot: BotAttributionIdentity) -> bool:
    bot_id_str = (bot.id_str or "").strip()
    return bool(bot_id_str and actor_id and actor_id == bot_id_str)


def _event_actor_matches_bot_slug(actor_login: str, bot: BotAttributionIdentity) -> bool:
    bot_slug = (bot.slug or "").strip()
    return bool(bot_slug and actor_login and actor_login.lower() == bot_slug.lower())


def _event_actor_matches_bot_uuid_fragments(
    actor_id: str, actor_login: str, bot: BotAttributionIdentity
) -> bool:
    bot_uuid = _normalize_scm_identity_fragment(bot.uuid)
    if not bot_uuid:
        return False
    actor_uuid = _normalize_scm_identity_fragment(actor_id)
    if actor_uuid and bot_uuid == actor_uuid:
        return True
    actor_login_uuid = _normalize_scm_identity_fragment(actor_login)
    return bool(actor_login_uuid and bot_uuid == actor_login_uuid)


def _reply_added_event_authored_by_bot(
    event: ReviewDecisionEventContext, bot: BotAttributionIdentity
) -> bool:
    """True when event actor fields identify the same user as the review token (bot)."""
    if not bot.is_resolved():
        return False
    actor_login = (event.actor_login or "").strip()
    actor_id = (event.actor_id or "").strip()
    if not actor_login and not actor_id:
        return False
    if _event_actor_matches_bot_login(actor_login, bot):
        return True
    if _event_actor_matches_bot_id(actor_id, bot):
        return True
    if _event_actor_matches_bot_slug(actor_login, bot):
        return True
    return _event_actor_matches_bot_uuid_fragments(actor_id, actor_login, bot)


def _reply_dismissal_entry_is_bot_authored(
    author_login: str,
    bot: BotAttributionIdentity,
) -> bool:
    """Best-effort match for thread entries, which usually expose only a login/slug-like field."""
    actor_login = (author_login or "").strip()
    if not actor_login or not bot.is_resolved():
        return False
    if _event_actor_matches_bot_login(actor_login, bot):
        return True
    if _event_actor_matches_bot_slug(actor_login, bot):
        return True
    return _event_actor_matches_bot_uuid_fragments("", actor_login, bot)


def _reply_dismissal_original_comment_id(
    ctx: ReviewThreadDismissalContext,
    bot: BotAttributionIdentity,
) -> str:
    """Prefer the first bot-authored entry; otherwise fall back to the first thread entry."""
    for ent in ctx.entries:
        if _reply_dismissal_entry_is_bot_authored(ent.author_login, bot):
            return (ent.comment_id or "").strip()
    if ctx.entries:
        return (ctx.entries[0].comment_id or "").strip()
    return ""


def _reply_dismissal_original_comment_severity(
    ctx: ReviewThreadDismissalContext,
    bot: BotAttributionIdentity,
) -> str:
    """Infer severity from the original automated review comment body when possible."""
    original_comment_id = _reply_dismissal_original_comment_id(ctx, bot)
    for ent in ctx.entries:
        if (ent.comment_id or "").strip() == original_comment_id:
            return infer_severity_from_comment_body(ent.body or "")
    if ctx.entries:
        return infer_severity_from_comment_body(ctx.entries[0].body or "")
    return "unknown"


def _reply_dismissal_existing_bot_reply_after_trigger(
    ctx: ReviewThreadDismissalContext,
    bot: BotAttributionIdentity,
    triggering_comment_id: str,
):
    """Return a later bot-authored thread entry when this trigger was already handled."""
    triggered_comment_id = (triggering_comment_id or "").strip()
    if not triggered_comment_id:
        return None
    seen_trigger = False
    for ent in ctx.entries:
        cid = (ent.comment_id or "").strip()
        if cid and cid == triggered_comment_id:
            seen_trigger = True
            continue
        if seen_trigger and _reply_dismissal_entry_is_bot_authored(ent.author_login, bot):
            return ent
    return None


def _reply_dismissal_scm_already_addressed_reason(
    ctx: ReviewThreadDismissalContext,
) -> str:
    """Provider-supplied reason when SCM already indicates the concern is addressed."""
    if not bool(getattr(ctx, "scm_already_addressed", False)):
        return ""
    return (getattr(ctx, "scm_already_addressed_reason", "") or "").strip() or "scm_state"


def _reply_dismissal_diff_context_for_thread(
    full_diff: str,
    ctx: ReviewThreadDismissalContext,
) -> str:
    """Return an annotated diff snippet for the thread's anchored file/line when available."""
    path = (ctx.path or "").strip()
    if not full_diff or not path:
        return ""
    line = int(ctx.line or 0)
    diff_text = get_diff_hunk_for_line(full_diff, path, line) if line > 0 else None
    if not diff_text:
        diff_text = unified_diff_for_path(full_diff, path)
    diff_text = (diff_text or "").strip()
    if not diff_text:
        return ""
    annotated = annotate_diff_with_line_numbers(diff_text)
    if len(annotated) > 12_000:
        annotated = annotated[:11_999] + "…"
    lines = [
        "",
        "Relevant PR diff context:",
        f"Anchored file: {path}",
    ]
    if line > 0:
        lines.append(f"Anchored line: {line}")
    lines.extend(
        [
            "",
            "```diff",
            annotated,
            "```",
        ]
    )
    return "\n".join(lines)


def _reply_dismissal_entry_tags(
    ent,
    *,
    original_comment_id: str,
    triggered_comment_id: str,
    bot: BotAttributionIdentity,
) -> list[str]:
    tags: list[str] = []
    cid = (ent.comment_id or "").strip()
    if cid and cid == original_comment_id:
        tags.append("original automated review comment")
    if cid and cid == triggered_comment_id:
        tags.append("triggering human reply")
    if _reply_dismissal_entry_is_bot_authored(ent.author_login, bot):
        tags.append("bot-authored")
    return tags


def _reply_dismissal_entry_lines(
    ent,
    index: int,
    *,
    original_comment_id: str,
    triggered_comment_id: str,
    bot: BotAttributionIdentity,
) -> list[str]:
    lines = [f"--- Comment {index} ---"]
    tags = _reply_dismissal_entry_tags(
        ent,
        original_comment_id=original_comment_id,
        triggered_comment_id=triggered_comment_id,
        bot=bot,
    )
    cid = (ent.comment_id or "").strip()
    if tags:
        lines.append(f"Role: {', '.join(tags)}")
    if cid:
        lines.append(f"Comment id: {cid}")
    lines.append(f"Author: {(ent.author_login or '').strip() or '(unknown)'}")
    lines.append(ent.body or "")
    lines.append("")
    return lines


def _format_reply_dismissal_user_message(
    ctx: ReviewThreadDismissalContext,
    bot: BotAttributionIdentity,
    triggering_comment_id: str,
    diff_context: str = "",
) -> str:
    """Build the user message for the reply-dismissal agent."""
    who = (bot.login or bot.slug or bot.id_str or bot.uuid or "").strip() or "(unknown)"
    original_comment_id = _reply_dismissal_original_comment_id(ctx, bot)
    original_comment_severity = _reply_dismissal_original_comment_severity(ctx, bot)
    triggered_comment_id = (triggering_comment_id or "").strip()
    lines = [
        "Classify this single pull-request review thread.",
        f"Automated reviewer identity hint (token user): {who}",
        f"Original automated review comment id: {original_comment_id or '(unknown)'}",
        f"Original automated review comment severity: {original_comment_severity}",
        f"Triggering human reply comment id: {triggered_comment_id or '(unknown)'}",
        "",
        "Thread comments in chronological order:",
    ]
    for i, ent in enumerate(ctx.entries, start=1):
        lines.extend(
            _reply_dismissal_entry_lines(
                ent,
                i,
                original_comment_id=original_comment_id,
                triggered_comment_id=triggered_comment_id,
                bot=bot,
            )
        )
    if diff_context:
        lines.append(diff_context)
    return "\n".join(lines)


def _run_reply_dismissal_llm(user_message: str) -> str:
    """Run the tool-free reply-dismissal agent once; return raw model text."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    from code_review.agent.reply_dismissal_agent import create_reply_dismissal_agent

    agent = create_reply_dismissal_agent()
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )
    session_id = f"reply-dismissal/{uuid.uuid4().hex[:12]}"
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "LLM request (reply-dismissal) session=%s prompt=%s",
            session_id,
            user_message,
        )
    content = types.Content(role="user", parts=[types.Part(text=user_message)])
    return _run_agent_and_collect_response(runner, session_service, session_id, content)


__all__ = [
    name
    for name in globals()
    if name != "__all__" and not (name.startswith("__") and name.endswith("__"))
]
