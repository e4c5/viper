"""ADK Runner setup and programmatic invocation for code review."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
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
from code_review.formatters.comment import finding_to_comment_body
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
from code_review.schemas.findings import FindingV1
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
) -> str:
    """Idempotency key: same key => same run already done for this PR/head/config."""
    config_hash = hashlib.sha256(
        f"{scm_cfg.provider}:{scm_cfg.url}:{llm_cfg.provider}:{llm_cfg.model}".encode()
    ).hexdigest()[:16]
    return (
        f"{scm_cfg.provider}/{owner}/{repo}/pr/{pr_number}/head/{head_sha}/"
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
    to_post: list[tuple[FindingV1, str]],
    cfg,
    llm_cfg,
    full_diff: str = "",
) -> int:
    """Build inline comments and post each one individually. Returns successful post count."""
    caps = provider.capabilities()
    run_id = _build_idempotency_key(cfg, llm_cfg, owner, repo, pr_number, head_sha)
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
        lines.append(
            f"It **identified {findings_planned} issue(s)** worth addressing on the diff."
        )
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
        run_id = _build_idempotency_key(cfg, llm_cfg, owner, repo, pr_number, head_sha)
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
        "review_decision_event_context source=%s "
        "comment_id=%s thread_id=%s actor_login=%s",
        ctx.source,
        ctx.comment_id,
        ctx.thread_id,
        ctx.actor_login,
    )


def _head_sha_hint_for_decision_only(
    ctx: ReviewDecisionEventContext | None,
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


def _parse_findings_json(text: str) -> list[dict]:
    """Extract JSON array from agent response; may be wrapped in markdown code block."""
    text = text.strip()
    # Strip ```json ... ``` or ``` ... ```
    for pattern in (r"```(?:json)?\s*([\s\S]*?)\s*```", r"\[[\s\S]*\]"):
        m = re.search(pattern, text)
        if m:
            raw = m.group(1).strip() if "```" in pattern else m.group(0)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def _findings_from_response(response_text: str) -> list[FindingV1]:
    """Parse response text into validated FindingV1 list. Invalid items skipped."""
    raw = _parse_findings_json(response_text)
    # Accept single finding as object: model may return {} instead of [{}]
    if isinstance(raw, dict):
        raw = [raw]
    findings: list[FindingV1] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            # Normalize keys: anchor -> fingerprint_hint if needed
            if "anchor" in item and "fingerprint_hint" not in item:
                item = {**item, "fingerprint_hint": item.get("anchor")}
            findings.append(FindingV1.model_validate(item))
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


def _format_reply_dismissal_user_message(
    ctx: ReviewThreadDismissalContext,
    bot: BotAttributionIdentity,
) -> str:
    """Build the user message for the reply-dismissal agent."""
    who = (bot.login or bot.slug or bot.id_str or bot.uuid or "").strip() or "(unknown)"
    lines = [
        "Classify this single pull-request review thread.",
        f"Automated reviewer identity hint (token user): {who}",
        "",
        "Thread comments in chronological order:",
    ]
    for i, ent in enumerate(ctx.entries, start=1):
        lines.append(f"--- Comment {i} ---")
        lines.append(f"Author: {(ent.author_login or '').strip() or '(unknown)'}")
        lines.append(ent.body or "")
        lines.append("")
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


class ReviewOrchestrator:
    """Orchestrates a single code review run (findings-only mode)."""

    def __init__(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str = "",
        *,
        dry_run: bool = False,
        print_findings: bool = False,
        review_decision_enabled: bool | None = None,
        review_decision_high_threshold: int | None = None,
        review_decision_medium_threshold: int | None = None,
        review_decision_only: bool = False,
        event_context: ReviewDecisionEventContext | None = None,
    ):
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.head_sha = head_sha
        self.dry_run = dry_run
        self.print_findings = print_findings
        self._review_decision_enabled_override = review_decision_enabled
        self._review_decision_high_threshold_override = review_decision_high_threshold
        self._review_decision_medium_threshold_override = review_decision_medium_threshold
        self._review_decision_only = review_decision_only
        self._event_context = event_context

    def _load_config_and_provider(self):
        """Load SCM/LLM config and create the provider instance.

        Returns (cfg, llm_cfg, provider).
        """
        cfg = get_scm_config()
        overrides: dict[str, bool | int] = {}
        if self._review_decision_enabled_override is not None:
            overrides["review_decision_enabled"] = self._review_decision_enabled_override
        if self._review_decision_high_threshold_override is not None:
            overrides["review_decision_high_threshold"] = (
                self._review_decision_high_threshold_override
            )
        if self._review_decision_medium_threshold_override is not None:
            overrides["review_decision_medium_threshold"] = (
                self._review_decision_medium_threshold_override
            )
        if overrides:
            cfg = cfg.model_copy(update=overrides)
        llm_cfg = get_llm_config()
        token_val = (
            cfg.token.get_secret_value() if hasattr(cfg.token, "get_secret_value") else cfg.token
        )
        provider = get_provider(
            cfg.provider,
            cfg.url,
            token_val,
            bitbucket_server_user_slug=cfg.bitbucket_server_user_slug,
        )
        return (cfg, llm_cfg, provider)

    def _determine_skip_reason(
        self,
        provider,
        cfg,
        owner: str,
        repo: str,
        pr_number: int,
        trace_id: str,
        start_time: float,
        run_handle,
    ) -> list[FindingV1] | None:
        """
        If the PR should be skipped (skip label or title pattern), emit observability and return [].
        Otherwise return None (caller continues).
        """
        if not cfg.skip_label and not cfg.skip_title_pattern:
            return None
        pr_info = provider.get_pr_info(owner, repo, pr_number)
        if not pr_info:
            return None
        if (
            cfg.skip_label
            and cfg.skip_label.strip()
            and any(lb.strip().lower() == cfg.skip_label.strip().lower() for lb in pr_info.labels)
        ):
            _duration_ms = (time.perf_counter() - start_time) * 1000
            _log_run_complete(trace_id, owner, repo, pr_number, 0, 0, 0, _duration_ms)
            observability.finish_run(
                run_handle, owner, repo, pr_number, 0, 0, 0, _duration_ms / 1000.0
            )
            return []
        if (
            cfg.skip_title_pattern
            and cfg.skip_title_pattern.strip()
            and cfg.skip_title_pattern.strip().lower() in pr_info.title.lower()
        ):
            _duration_ms = (time.perf_counter() - start_time) * 1000
            _log_run_complete(trace_id, owner, repo, pr_number, 0, 0, 0, _duration_ms)
            observability.finish_run(
                run_handle, owner, repo, pr_number, 0, 0, 0, _duration_ms / 1000.0
            )
            return []
        return None

    def _load_existing_comments_and_markers(self, provider, owner: str, repo: str, pr_number: int):
        """
        Fetch existing review comments, build ignore set and resolved sets from markers.
        Returns (existing, existing_dicts, ignore_set, resolved_comments,
                 resolved_body_set, resolved_fp_set).
        """
        existing = provider.get_existing_review_comments(owner, repo, pr_number)
        existing_dicts = [c.model_dump() for c in existing]
        ignore_set = _build_ignore_set(existing_dicts)
        resolved_comments = []
        for c in existing:
            resolved_flag = getattr(c, "resolved", False)
            if isinstance(resolved_flag, bool) and resolved_flag:
                resolved_comments.append(c)
        resolved_body_set: set[tuple[str, str]] = set()
        resolved_fp_set: set[tuple[str, str]] = set()
        for c in resolved_comments:
            path = getattr(c, "path", "") or ""
            body = getattr(c, "body", "") or ""
            if not path or not body:
                continue
            body_hash = hashlib.sha256(body.encode()).hexdigest()
            resolved_body_set.add((path, body_hash))
            parsed = parse_marker_from_comment_body(body)
            if parsed.get("fingerprint"):
                resolved_fp_set.add((path, parsed["fingerprint"]))
        return (
            existing,
            existing_dicts,
            ignore_set,
            resolved_comments,
            resolved_body_set,
            resolved_fp_set,
        )

    def _compute_idempotency_and_maybe_short_circuit(
        self,
        cfg,
        llm_cfg,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        existing_dicts: list,
        trace_id: str,
        start_time: float,
        run_handle,
    ) -> list[FindingV1] | None:
        """
        If we already ran for this PR/head/config (run id in comment marker),
        emit observability and return []. Otherwise return None (caller continues).
        """
        if not head_sha:
            return None
        run_id = _build_idempotency_key(cfg, llm_cfg, owner, repo, pr_number, head_sha)
        if not _idempotency_key_seen_in_comments(existing_dicts, run_id):
            return None
        _duration_ms = (time.perf_counter() - start_time) * 1000
        _log_run_complete(trace_id, owner, repo, pr_number, 0, 0, 0, _duration_ms)
        observability.finish_run(run_handle, owner, repo, pr_number, 0, 0, 0, _duration_ms / 1000.0)
        return []

    @staticmethod
    def _incremental_base_sha(cfg, head_sha: str) -> str:
        """Return a usable incremental review base SHA, else ``""`` for full-PR review."""
        raw_base_sha = getattr(cfg, "base_sha", "")
        base_sha = raw_base_sha.strip() if isinstance(raw_base_sha, str) else ""
        head_sha = (head_sha or "").strip()
        if not base_sha or not head_sha or base_sha == head_sha:
            return ""
        return base_sha

    def _fetch_review_files_and_diffs(
        self,
        provider,
        cfg,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
    ) -> tuple[list[object], list[str], str, str]:
        """Fetch the file list and diff for the active review scope.

        Returns ``(files, paths, full_diff, incremental_base_sha)`` where
        ``incremental_base_sha`` is non-empty only when the review was scoped to
        ``SCM_BASE_SHA..SCM_HEAD_SHA``.
        """
        base_sha = self._incremental_base_sha(cfg, head_sha)
        if base_sha:
            files = provider.get_incremental_pr_files(owner, repo, pr_number, base_sha, head_sha)
            paths = [f.path for f in files]
            full_diff = provider.get_incremental_pr_diff(owner, repo, pr_number, base_sha, head_sha)
            return (files, paths, full_diff, base_sha)
        files = provider.get_pr_files(owner, repo, pr_number)
        paths = [f.path for f in files]
        full_diff = provider.get_pr_diff(owner, repo, pr_number)
        return (files, paths, full_diff, "")

    def _build_ignore_set_and_filter_files(self, paths: list[str]) -> list[str]:
        """
        Optionally filter which file paths to review (e.g. by ignore patterns).
        Currently returns paths unchanged; ignore_set is built in
        _load_existing_comments_and_markers and used later to filter findings.
        """
        return paths

    def _detect_languages_for_files(self, paths: list[str]):
        """Run language detection on paths and return (detected, review_standards)."""
        detected = detect_from_paths(paths)
        review_standards = get_review_standards(detected.language, detected.framework)
        return (detected, review_standards)

    def _create_agent_and_runner(
        self,
        provider,
        review_standards: str,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        use_file_by_file: bool = False,
        disable_tools: bool | None = None,
        context_brief_attached: bool = False,
    ):
        """
        Build the findings-only agent, session service, and ADK Runner.
        Returns (session_id, session_service, runner).

        IMPORTANT: this method must be called AFTER determining the execution mode,
        because the mode directly controls tool and instruction configuration:

        - Single-shot mode (use_file_by_file=False): tools are disabled and the
          SINGLE_SHOT_INSTRUCTION is used.  The full diff is already embedded in
          the user message — no tools are needed.  Enabling tools here causes the
          LLM to make per-file tool calls; each call appends to the ADK session
          history, and every subsequent LLM turn re-bills all prior context
          (triangular token growth → millions of billed tokens on large PRs).
          Additionally, FINDINGS_ONLY_INSTRUCTION references tool names that are
          absent in this mode; Gemini infers it cannot complete the workflow and
          returns [] (no findings).

        - File-by-file mode (use_file_by_file=True): tools are enabled and
          FINDINGS_ONLY_INSTRUCTION is used.  The agent calls get_pr_diff_for_file
          per file, which is the expected workflow.

        - Incremental large-diff mode may still set ``use_file_by_file=True`` but
          also force ``disable_tools=True`` so each per-file prompt embeds the
          already-scoped diff slice rather than fetching the file's full PR diff.
        """
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        disable_tools_effective = disable_tools if disable_tools is not None else not use_file_by_file
        agent = create_review_agent(
            provider,
            review_standards,
            findings_only=True,
            disable_tools=disable_tools_effective,
            context_brief_attached=context_brief_attached,
        )
        session_id = f"{owner}/{repo}/pr-{pr_number}/{uuid.uuid4().hex[:12]}"
        session_service = InMemorySessionService()
        runner = Runner(
            agent=agent,
            app_name=APP_NAME,
            session_service=session_service,
        )
        return (session_id, session_service, runner)

    def _run_agent_and_collect_findings(
        self,
        runner,
        session_service,
        session_id: str,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        paths: list[str],
        use_file_by_file: bool,
        full_diff: str = "",
        prompt_suffix: str = "",
        diff_by_path: dict[str, str] | None = None,
    ) -> list[FindingV1]:
        """
        Run the agent (file-by-file or single shot), parse response into FindingV1 list.
        Returns all_findings (unfiltered).

        When CODE_REVIEW_LOG_LEVEL=DEBUG, log exactly what we send to the LLM
        (the user message text) and the raw text we receive back, before any
        JSON parsing or filtering.
        """
        if use_file_by_file and paths:
            if diff_by_path is not None:
                return self._run_embedded_file_by_file_mode(
                    runner,
                    session_service,
                    owner,
                    repo,
                    pr_number,
                    head_sha,
                    paths,
                    diff_by_path=diff_by_path,
                    prompt_suffix=prompt_suffix,
                )
            return self._run_file_by_file_mode(
                runner,
                session_service,
                owner,
                repo,
                pr_number,
                head_sha,
                paths,
                prompt_suffix=prompt_suffix,
            )
        return self._run_single_shot_mode(
            runner,
            session_service,
            session_id,
            owner,
            repo,
            pr_number,
            head_sha,
            full_diff,
            prompt_suffix=prompt_suffix,
        )

    def _run_embedded_file_by_file_mode(
        self,
        runner,
        session_service,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        paths: list[str],
        *,
        diff_by_path: dict[str, str],
        prompt_suffix: str = "",
    ) -> list[FindingV1]:
        """Review one file at a time using already-scoped embedded diffs (tools disabled)."""
        all_findings: list[FindingV1] = []
        for file_path in paths:
            file_session_id = f"{owner}/{repo}/pr-{pr_number}/file/{uuid.uuid4().hex[:12]}"
            file_diff = diff_by_path.get(file_path, "")
            if not file_diff:
                continue
            head_sha_clause = f" head_sha={head_sha}." if head_sha else ""
            msg = (
                "Review exactly one file from this PR. "
                f"owner={owner}, repo={repo}, pr_number={pr_number}."
                + head_sha_clause
                + f' Use path "{file_path}" in every finding. '
                "The diff below is already scoped to the current review range for this file. "
                "Output a JSON array of findings for this file only. "
                "If there are no issues in this file, output exactly []."
            )
            annotated = annotate_diff_with_line_numbers(file_diff)
            msg += f"\n\nHere is the unified diff for this file:\n```diff\n{annotated}\n```"
            if prompt_suffix:
                msg = msg + "\n\n" + prompt_suffix
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "LLM request (embedded file-by-file) session=%s file=%s prompt=%s",
                    file_session_id,
                    file_path,
                    msg,
                )
            content = types.Content(role="user", parts=[types.Part(text=msg)])
            try:
                response_text = _run_agent_and_collect_response(
                    runner, session_service, file_session_id, content
                )
            except AuthenticationError as e:
                logger.error(
                    "LLM authentication failed while reviewing file=%s; aborting run: %s",
                    file_path,
                    e,
                )
                raise
            except RateLimitError as e:
                logger.warning(
                    "Rate limit hit while reviewing file=%s (skipping): %s",
                    file_path,
                    e,
                )
                continue
            except Exception as e:
                logger.warning(
                    "Error reviewing file=%s (skipping): %s",
                    file_path,
                    e,
                    exc_info=logger.isEnabledFor(logging.DEBUG),
                )
                continue
            all_findings.extend(_findings_from_response(response_text))
        return all_findings

    def _run_file_by_file_mode(
        self,
        runner,
        session_service,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        paths: list[str],
        *,
        prompt_suffix: str = "",
    ) -> list[FindingV1]:
        all_findings: list[FindingV1] = []
        for file_path in paths:
            file_session_id = f"{owner}/{repo}/pr-{pr_number}/file/{uuid.uuid4().hex[:12]}"
            head_sha_clause = f" head_sha={head_sha}." if head_sha else ""
            ref_guidance = (
                f' When calling get_file_lines for surrounding context, use ref="{head_sha}"'
                " as the ref parameter."
                if head_sha
                else ""
            )
            annotation_guidance = (
                " The diff returned by get_pr_diff_for_file has <L{n}> line-number"
                " annotations on every visible line (added '+' and context ' ')."
                " Use the <L{n}> value as the 'line' field in each finding."
                " Do NOT compute line numbers from hunk headers."
            )
            msg = (
                "Review exactly one file from this PR. "
                f"owner={owner}, repo={repo}, pr_number={pr_number}."
                + head_sha_clause
                + " Call get_pr_diff_for_file(owner, repo, pr_number, "
                + f'"{file_path}") to get the diff for this file.'
                + annotation_guidance
                + ref_guidance
                + " Then output a JSON array of findings for this file only. "
                + f'Use path "{file_path}" in every finding. '
                "If there are no issues in this file, output exactly []."
            )
            if prompt_suffix:
                msg = msg + "\n\n" + prompt_suffix
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "LLM request (file-by-file) session=%s file=%s prompt=%s",
                    file_session_id,
                    file_path,
                    msg,
                )
            content = types.Content(role="user", parts=[types.Part(text=msg)])
            try:
                response_text = _run_agent_and_collect_response(
                    runner, session_service, file_session_id, content
                )
            except AuthenticationError as e:
                # LLM authentication errors (e.g. HTTP 401 from OpenRouter/OpenAI/etc.)
                # indicate a misconfigured or missing API key. Treat these as fatal so
                # CI surfaces them clearly instead of silently skipping all files.
                logger.error(
                    "LLM authentication failed while reviewing file=%s; aborting run: %s",
                    file_path,
                    e,
                )
                raise
            except RateLimitError as e:
                logger.warning(
                    "Rate limit hit while reviewing file=%s (skipping): %s",
                    file_path,
                    e,
                )
                continue
            except Exception as e:
                logger.warning(
                    "Error reviewing file=%s (skipping): %s",
                    file_path,
                    e,
                    exc_info=logger.isEnabledFor(logging.DEBUG),
                )
                continue
            all_findings.extend(_findings_from_response(response_text))
        return all_findings

    def _run_single_shot_mode(
        self,
        runner,
        session_service,
        session_id: str,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        full_diff: str,
        *,
        prompt_suffix: str = "",
    ) -> list[FindingV1]:
        # Single-shot mode: include the unified diff directly in the prompt so the
        # LLM can review the changes even if tool calls are unavailable or flaky.
        # We only reach this branch when the diff fits within the configured
        # DIFF_TOKEN_BUDGET_RATIO, so including the full diff is safe.
        #
        # Annotate the diff with explicit <L{n}> new-file line numbers before
        # embedding it in the prompt.  Without annotations the LLM has to derive
        # absolute line numbers from hunk headers by counting +/- lines — a
        # calculation it frequently gets wrong, causing comments to land on the
        # wrong lines in the diff view.
        msg = f"Review this PR: owner={owner}, repo={repo}, pr_number={pr_number}." + (
            f" head_sha={head_sha}." if head_sha else ""
        )
        if full_diff:
            annotated = annotate_diff_with_line_numbers(full_diff)
            msg += f"\n\nHere is the unified diff for this PR:\n```diff\n{annotated}\n```"
        if prompt_suffix:
            msg += "\n\n" + prompt_suffix
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "LLM request (single-shot) session=%s prompt=%s",
                session_id,
                msg,
            )
        content = types.Content(role="user", parts=[types.Part(text=msg)])
        response_text = _run_agent_and_collect_response(
            runner, session_service, session_id, content
        )
        return _findings_from_response(response_text)

    def _attach_fingerprints_and_filter_findings(
        self,
        all_findings: list[FindingV1],
        provider,
        owner: str,
        repo: str,
        head_sha: str,
        ignore_set: set[tuple[str, str]],
        resolved_body_set: set[tuple[str, str]],
        resolved_fp_set: set[tuple[str, str]],
    ) -> list[tuple[FindingV1, str]]:
        """
        Attach fingerprints to findings, filter by ignore/resolved sets.
        Mutates ignore_set (adds new keys). Returns to_post: list of (finding, fingerprint).
        """
        to_post: list[tuple[FindingV1, str]] = []
        unique_paths = list(dict.fromkeys(f.path for f in all_findings))
        file_lines_by_path = (
            _get_file_lines_by_path(provider, owner, repo, head_sha, unique_paths)
            if head_sha
            else {}
        )
        for f in all_findings:
            body = finding_to_comment_body(f)
            body_hash = hashlib.sha256(body.encode()).hexdigest()
            fp = _fingerprint_for_finding(f, file_lines_by_path) if file_lines_by_path else ""
            if _should_skip_finding_for_dedup(
                f.path, body_hash, fp, ignore_set, resolved_body_set, resolved_fp_set
            ):
                continue
            if fp:
                ignore_set.add((f.path, fp))
            ignore_set.add((f.path, body_hash))
            to_post.append((f, fp))
        return to_post

    def _post_findings_and_summary(
        self,
        provider,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        dry_run: bool,
        to_post: list[tuple[FindingV1, str]],
        cfg,
        llm_cfg,
        existing: list,
        full_diff: str = "",
    ) -> int:
        """
        Auto-resolve stale comments (if supported), then post inline comments.
        Returns successful_post_count.
        full_diff: used to set line_type (ADDED vs CONTEXT) so Bitbucket
        Server anchors comments correctly.
        """
        _resolve_stale_comments_if_supported(
            provider, owner, repo, pr_number, existing, to_post, head_sha, dry_run
        )
        if dry_run:
            return 0
        gate_outcome = _compute_quality_gate_review_outcome(
            provider, owner, repo, pr_number, to_post, cfg
        )
        count = 0
        if to_post:
            if not head_sha:
                raise ValueError(
                    "head_sha is required when posting comments (dry_run=False). "
                    "Provide head_sha or use --dry-run to skip posting."
                )
            count = _post_inline_comments(
                provider,
                owner,
                repo,
                pr_number,
                head_sha,
                to_post,
                cfg,
                llm_cfg,
                full_diff=full_diff,
            )
        # Bitbucket Server/Cloud: PR-level summary; run= marker only when inline posting
        # fully succeeded (or nothing was planned) so partial failures can retry on the next run.
        if head_sha and provider.capabilities().omit_fingerprint_marker_in_body:
            planned = len(to_post)
            include_marker = planned == 0 or count == planned
            _post_omit_marker_pr_summary_comment(
                provider,
                owner,
                repo,
                pr_number,
                cfg,
                llm_cfg,
                head_sha,
                findings_planned=planned,
                successful_inline_posts=count,
                gate_outcome=gate_outcome,
                include_run_marker=include_marker,
            )
        _maybe_submit_review_decision(
            provider,
            owner,
            repo,
            pr_number,
            head_sha,
            dry_run,
            cfg,
            gate_outcome=gate_outcome,
        )
        return count

    def _record_observability_and_build_result(
        self,
        trace_id: str,
        owner: str,
        repo: str,
        pr_number: int,
        start_time: float,
        run_handle,
        paths: list,
        all_findings: list[FindingV1],
        successful_post_count: int,
        to_post: list[tuple[FindingV1, str]],
        context_brief_attached: bool = False,
    ) -> list[FindingV1]:
        """
        Emit run_complete log and observability.finish_run, then return the list of findings posted.
        """
        _duration_ms = (time.perf_counter() - start_time) * 1000
        _log_run_complete(
            trace_id,
            owner,
            repo,
            pr_number,
            files_count=len(paths),
            findings_count=len(all_findings),
            posts_count=successful_post_count,
            duration_ms=_duration_ms,
        )
        observability.finish_run(
            run_handle,
            owner,
            repo,
            pr_number,
            files_count=len(paths),
            findings_count=len(all_findings),
            posts_count=successful_post_count,
            duration_seconds=_duration_ms / 1000.0,
            context_brief_attached=context_brief_attached,
        )
        return [f for f, _ in to_post]

    @staticmethod
    def _print_findings_summary(print_findings: bool, to_post: list[tuple[FindingV1, str]]) -> None:
        if not print_findings:
            return
        if to_post:
            print("\n" + "=" * 60)
            print(f"Code Review Findings ({len(to_post)} total)")
            print("=" * 60)
            for f, _ in to_post:
                line_info = (
                    f"{f.line}"
                    if not f.end_line or f.end_line == f.line
                    else f"{f.line}-{f.end_line}"
                )
                print(f"[{f.severity.upper()}] {f.path}:{line_info}")
                print(f"Message: {f.get_body()}")
                if f.suggested_patch:
                    print("-" * 40)
                    print("Suggested Patch:")
                    print(f.suggested_patch)
                print("=" * 60)
            print()
        else:
            print("No findings to post.")

    @staticmethod
    def _log_post_counts(dry_run: bool, planned_count: int, successful_post_count: int) -> None:
        if dry_run:
            logger.info("Dry run: would post %d comment(s)", planned_count)
        else:
            logger.info("Posted %d comment(s)", successful_post_count)

    @staticmethod
    def _filter_findings_to_pr_paths(
        findings: list[FindingV1], paths: list[str]
    ) -> list[FindingV1]:
        if not paths:
            return findings
        allowed_normalized = {_normalize_path_for_anchor(p) for p in paths}
        filtered_findings: list[FindingV1] = []
        for finding in findings:
            norm_path = _normalize_path_for_anchor(finding.path or "")
            if norm_path in allowed_normalized:
                filtered_findings.append(finding)
            elif logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Dropping finding for path not in diff: %s (normalized=%s, allowed=%s)",
                    finding.path,
                    norm_path,
                    sorted(allowed_normalized),
                )
        return filtered_findings

    @staticmethod
    def _filter_findings_to_visible_diff_lines(
        findings: list[FindingV1], full_diff: str
    ) -> list[FindingV1]:
        if not full_diff:
            return findings
        visible_lines = _diff_visible_new_lines(full_diff)
        if not visible_lines:
            return findings
        line_filtered: list[FindingV1] = []
        for finding in findings:
            norm_path = _normalize_path_for_anchor(finding.path or "")
            if (norm_path, finding.line) in visible_lines:
                line_filtered.append(finding)
            else:
                logger.debug(
                    "Dropping finding for line not visible in diff: %s:%d",
                    finding.path,
                    finding.line,
                )
        return line_filtered

    def _filter_findings_by_diff_scope(
        self, findings: list[FindingV1], paths: list[str], full_diff: str
    ) -> list[FindingV1]:
        # Guardrail: only keep findings for files that are actually in the PR diff.
        # LLMs can occasionally emit findings for unrelated paths; we never want to post
        # comments on files outside the reviewed change set.
        # Paths may include prefixes like dst:// or src:// (especially for Bitbucket);\
        # normalize both diff paths and finding paths before comparison so valid findings
        # are not dropped just because of a prefix difference.
        path_filtered = self._filter_findings_to_pr_paths(findings, paths)

        # Guardrail: relocate findings whose anchor/fingerprint_hint text doesn't
        # match the diff content at the reported line.  The LLM sometimes identifies
        # the right code issue but reports a line number that is off by a few lines.
        # Relocating before line-visibility filtering ensures corrected findings are
        # also validated against the visible diff range.
        relocated = _relocate_findings_by_anchor(path_filtered, full_diff)

        # Guardrail: only keep findings for lines that are actually visible in the diff.
        # Comments on lines outside diff hunks cannot be placed inline: Bitbucket Cloud
        # rejects them (causing fallback to PR-level activity comments) and GitHub/Gitea
        # reject them via position-based APIs. In single-shot mode the LLM receives the
        # full multi-file diff and may report lines that are in the file but outside any
        # hunk (e.g. unchanged code far from the changed region). Filtering here ensures
        # every posted comment appears inline in the diff view.
        line_filtered = self._filter_findings_to_visible_diff_lines(relocated, full_diff)

        # Guardrail: strip suggested_patch from findings where the patch content has no
        # token overlap with the actual diff line it is anchored to.  This catches the
        # common LLM error of hallucinating a patch for a different line of code while
        # naming a visible line (so the line-visibility guard above passes).  Without
        # this guard, the patch would be rendered as a suggestion block replacing the
        # wrong code.  Stripping it degrades gracefully to a plain inline comment.
        patched = _validate_suggested_patches(line_filtered, full_diff)
        # Drop findings where the model argued itself out of the issue in the same message.
        return _filter_self_retracted_finding_messages(patched)

    @staticmethod
    def _build_pr_url(cfg, owner: str, repo: str, pr_number: int) -> str:
        base_url = cfg.url.rstrip("/")
        if cfg.provider == "github":
            return f"{base_url}/{owner}/{repo}/pull/{pr_number}"
        if cfg.provider == "gitlab":
            return f"{base_url}/{owner}/{repo}/-/merge_requests/{pr_number}"
        if cfg.provider == "bitbucket":
            return f"https://bitbucket.org/{owner}/{repo}/pull-requests/{pr_number}"
        if cfg.provider == "bitbucket_server":
            return f"{base_url}/projects/{owner}/repos/{repo}/pull-requests/{pr_number}"
        return f"{base_url}/{owner}/{repo}/pulls/{pr_number}"

    @staticmethod
    def _load_commit_messages(
        provider, owner: str, repo: str, pr_number: int, need_commits: bool
    ) -> list[str]:
        raw = provider.get_pr_commit_messages(owner, repo, pr_number) if need_commits else []
        return raw if isinstance(raw, list) else []

    def _build_prompt_suffix(
        self,
        provider,
        cfg,
        ctx_cfg,
        app_cfg,
        owner: str,
        repo: str,
        pr_number: int,
        pr_info_for_metadata,
        full_diff: str,
        remaining_tokens: int,
    ) -> tuple[list[object], str | None, str]:
        need_commits = app_cfg.include_commit_messages_in_prompt or ctx_cfg.enabled
        commit_messages = self._load_commit_messages(provider, owner, repo, pr_number, need_commits)
        pr_title = (pr_info_for_metadata.title if pr_info_for_metadata else "") or ""
        pr_desc = (pr_info_for_metadata.description if pr_info_for_metadata else "") or ""
        refs = (
            extract_context_references(
                cfg.provider,
                owner,
                repo,
                [pr_title, pr_desc, *commit_messages],
                extract_github=ctx_cfg.github_issues_enabled,
                extract_gitlab=ctx_cfg.gitlab_issues_enabled,
                extract_jira=ctx_cfg.jira_enabled,
                extract_confluence=ctx_cfg.confluence_enabled,
            )
            if ctx_cfg.enabled
            else []
        )
        context_brief: str | None = None
        if ctx_cfg.enabled and refs:
            try:
                context_brief = build_context_brief_for_pr(ctx_cfg, cfg, refs, full_diff)
            except ContextAwareFatalError:
                logger.exception("Context-aware fetch or distillation failed")
                raise
        prompt_suffix = _format_review_prompt_supplement(
            context_brief=context_brief,
            commit_messages=commit_messages,
            include_commit_messages=app_cfg.include_commit_messages_in_prompt,
            remaining_tokens=remaining_tokens,
        )
        return refs, context_brief, prompt_suffix

    def _decision_only_try_skip_when_bot_not_blocking(
        self,
        provider,
        app_cfg,
        owner: str,
        repo: str,
        pr_number: int,
        trace_id: str,
        start_time: float,
        run_handle,
    ) -> list[FindingV1] | None:
        if not (
            app_cfg.review_decision_only_skip_if_bot_not_blocking
            and event_allows_decision_only_skip_when_bot_not_blocking(self._event_context)
        ):
            return None
        caps = provider.capabilities()
        if not caps.supports_bot_blocking_state_query:
            return None
        if provider.get_bot_blocking_state(owner, repo, pr_number) != "NOT_BLOCKING":
            return None
        logger.info(
            "Review-decision-only: skipping quality gate (bot not blocking; "
            "CODE_REVIEW_REVIEW_DECISION_ONLY_SKIP_IF_BOT_NOT_BLOCKING=1, "
            "comment_id present)."
        )
        return self._record_observability_and_build_result(
            trace_id,
            owner,
            repo,
            pr_number,
            start_time,
            run_handle,
            paths=[],
            all_findings=[],
            successful_post_count=0,
            to_post=[],
        )

    def _decision_only_maybe_post_disagreed_thread_reply(
        self,
        provider,
        caps_rd,
        owner: str,
        repo: str,
        pr_number: int,
        dry_run: bool,
        comment_id: str,
        verdict: ReplyDismissalVerdictV1,
    ) -> None:
        if not caps_rd.supports_review_thread_reply:
            return
        if dry_run:
            truncated = (verdict.reply_text or "")[:500]
            logger.info(
                "Dry-run: would post review-thread reply (truncated): %s",
                truncated,
            )
            return
        try:
            provider.post_review_thread_reply(
                owner, repo, pr_number, comment_id, verdict.reply_text
            )
        except Exception as e:
            logger.warning("post_review_thread_reply failed: %s", e)

    def _decision_only_reply_dismissal_excluded_gate_ids(
        self,
        provider,
        app_cfg,
        owner: str,
        repo: str,
        pr_number: int,
        dry_run: bool,
        trace_id: str,
    ) -> frozenset[str]:
        """Stable ids to exclude from the quality gate after optional reply-dismissal LLM."""
        ctx = self._event_context
        if not (
            app_cfg.reply_dismissal_enabled
            and ctx is not None
            and (ctx.comment_id or "").strip()
        ):
            if app_cfg.reply_dismissal_enabled and logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Reply-dismissal not run: requires CODE_REVIEW_EVENT_COMMENT_ID; "
                    "got comment_id=%r ctx_present=%s",
                    ((ctx.comment_id or "").strip() if ctx else "") or "",
                    ctx is not None,
                )
            return frozenset()

        comment_id = ctx.comment_id.strip()
        bot_id = provider.get_bot_attribution_identity(owner, repo, pr_number)
        if _reply_added_event_authored_by_bot(ctx, bot_id):
            observability.record_reply_dismissal_outcome("skipped_bot_author")
            logger.info(
                "Reply-dismissal skipped: reply_added actor matches bot "
                "(actor_login=%r actor_id=%r)",
                (ctx.actor_login or "").strip(),
                (ctx.actor_id or "").strip(),
            )
            return frozenset()

        caps_rd = provider.capabilities()
        if not caps_rd.supports_review_thread_dismissal_context:
            observability.record_reply_dismissal_outcome("skipped_no_capability")
            return frozenset()

        dctx = provider.get_review_thread_dismissal_context(
            owner, repo, pr_number, comment_id
        )
        if dctx is None or len(dctx.entries) < 2:
            observability.record_reply_dismissal_outcome("skipped_insufficient_thread")
            return frozenset()

        user_msg = _format_reply_dismissal_user_message(dctx, bot_id)
        try:
            raw_verdict = _run_reply_dismissal_llm(user_msg)
        except Exception as e:
            logger.warning("Reply-dismissal LLM run failed: %s", e)
            observability.record_reply_dismissal_outcome("llm_error")
            return frozenset()

        verdict = reply_dismissal_verdict_from_llm_text(raw_verdict)
        if verdict is None:
            observability.record_reply_dismissal_outcome("parse_failed")
            snippet = (raw_verdict or "").strip()
            if len(snippet) > 1500:
                snippet = snippet[:1500] + "…"
            logger.warning(
                "Reply-dismissal LLM output could not be parsed as ReplyDismissalVerdictV1; "
                "enable DEBUG for full request/response. Raw (truncated): %r",
                snippet or "(empty)",
            )
            return frozenset()

        logger.info(
            "reply_dismissal_verdict trace_id=%s verdict=%s pr=%s/%s#%s",
            trace_id,
            verdict.verdict,
            owner,
            repo,
            pr_number,
        )

        if verdict.verdict == "agreed":
            observability.record_reply_dismissal_outcome("agreed")
            logger.info(
                "Reply-dismissal agreed; excluding gate stable_id=%s",
                dctx.gate_exclusion_stable_id,
            )
            return frozenset({dctx.gate_exclusion_stable_id})

        if verdict.verdict == "disagreed":
            observability.record_reply_dismissal_outcome("disagreed")
            self._decision_only_maybe_post_disagreed_thread_reply(
                provider,
                caps_rd,
                owner,
                repo,
                pr_number,
                dry_run,
                comment_id,
                verdict,
            )

        return frozenset()

    def _run_review_decision_only(
        self,
        trace_id: str,
        start_time: float,
        run_handle,
        cfg,
        provider,
    ) -> list[FindingV1]:
        """Recompute quality-gate counts from SCM state and submit review decision only."""
        owner = self.owner
        repo = self.repo
        pr_number = self.pr_number
        dry_run = self.dry_run
        pr_url = self._build_pr_url(cfg, owner, repo, pr_number)
        logger.info(
            "Review-decision-only run for %s/%s PR %s (provider=%s) URL: %s",
            owner,
            repo,
            pr_number,
            cfg.provider,
            pr_url,
        )
        print(f"Review-decision-only for PR: {pr_url}")

        _log_review_decision_event_if_present(self._event_context)

        app_cfg = get_code_review_app_config()

        skip_result = self._determine_skip_reason(
            provider, cfg, owner, repo, pr_number, trace_id, start_time, run_handle
        )
        if skip_result is not None:
            return skip_result

        skip_early = self._decision_only_try_skip_when_bot_not_blocking(
            provider,
            app_cfg,
            owner,
            repo,
            pr_number,
            trace_id,
            start_time,
            run_handle,
        )
        if skip_early is not None:
            return skip_early

        head_hint = _head_sha_hint_for_decision_only(self._event_context, self.head_sha)
        head_sha = _resolve_head_sha_for_review_decision_submission(
            provider, owner, repo, pr_number, head_hint
        )
        if not head_sha and not dry_run:
            logger.warning(
                "Review-decision-only: head_sha missing after provider lookup; "
                "submit_review_decision may omit commit id for some SCMs."
            )

        excluded_gate = self._decision_only_reply_dismissal_excluded_gate_ids(
            provider, app_cfg, owner, repo, pr_number, dry_run, trace_id
        )

        gate_outcome = _compute_quality_gate_review_outcome(
            provider,
            owner,
            repo,
            pr_number,
            [],
            cfg,
            excluded_gate_stable_ids=excluded_gate if excluded_gate else None,
        )
        logger.info(
            "Review-decision-only quality gate: open_high=%d open_medium=%d => decision=%s",
            gate_outcome.high_count,
            gate_outcome.medium_count,
            gate_outcome.decision,
        )
        _maybe_submit_review_decision(
            provider,
            owner,
            repo,
            pr_number,
            head_sha,
            dry_run,
            cfg,
            gate_outcome=gate_outcome,
        )

        return self._record_observability_and_build_result(
            trace_id,
            owner,
            repo,
            pr_number,
            start_time,
            run_handle,
            paths=[],
            all_findings=[],
            successful_post_count=0,
            to_post=[],
        )

    def run(self) -> list[FindingV1]:
        """
        Execute the full review flow. Returns list of findings that were posted
        (or would be posted if dry_run).
        """
        # Unpack to locals for use in helper calls below.
        owner = self.owner
        repo = self.repo
        pr_number = self.pr_number
        head_sha = self.head_sha
        dry_run = self.dry_run
        print_findings = self.print_findings

        trace_id = str(uuid.uuid4())
        start_time = time.perf_counter()
        run_handle = observability.start_run(trace_id)

        cfg, llm_cfg, provider = self._load_config_and_provider()
        app_cfg = get_code_review_app_config()
        decision_only = bool(self._review_decision_only) or bool(app_cfg.review_decision_only)
        if decision_only:
            return self._run_review_decision_only(trace_id, start_time, run_handle, cfg, provider)

        pr_url = self._build_pr_url(cfg, owner, repo, pr_number)

        logger.info(
            "Reviewing %s/%s PR %s (provider=%s) URL: %s",
            owner,
            repo,
            pr_number,
            cfg.provider,
            pr_url,
        )
        print(f"Starting review for PR: {pr_url}")

        skip_result = self._determine_skip_reason(
            provider, cfg, owner, repo, pr_number, trace_id, start_time, run_handle
        )
        if skip_result is not None:
            return skip_result

        (
            existing,
            existing_dicts,
            ignore_set,
            resolved_comments,
            resolved_body_set,
            resolved_fp_set,
        ) = self._load_existing_comments_and_markers(provider, owner, repo, pr_number)

        idempotency_result = self._compute_idempotency_and_maybe_short_circuit(
            cfg,
            llm_cfg,
            owner,
            repo,
            pr_number,
            head_sha,
            existing_dicts,
            trace_id,
            start_time,
            run_handle,
        )
        if idempotency_result is not None:
            logger.info("Skipping run (idempotent: same head/config already reviewed)")
            return idempotency_result

        ctx_cfg = get_context_aware_config()
        if ctx_cfg.enabled:
            try:
                validate_context_aware_sources(ctx_cfg, cfg)
            except ContextAwareFatalError as e:
                logger.error("Context-aware review configuration error: %s", e)
                observability.finish_run(
                    run_handle,
                    owner,
                    repo,
                    pr_number,
                    files_count=0,
                    findings_count=0,
                    posts_count=0,
                    duration_seconds=(time.perf_counter() - start_time),
                )
                raise

        pr_info_for_metadata = provider.get_pr_info(owner, repo, pr_number)

        _, paths, full_diff, incremental_base_sha = self._fetch_review_files_and_diffs(
            provider, cfg, owner, repo, pr_number, head_sha
        )
        paths = self._build_ignore_set_and_filter_files(paths)
        if incremental_base_sha:
            logger.info(
                "Fetched incremental diff base=%s head=%s, %d file(s) to review",
                incremental_base_sha[:12],
                (head_sha or "")[:12],
                len(paths),
            )
        else:
            logger.info("Fetched diff, %d file(s) to review", len(paths))
        if not paths:
            logger.info("No files to review, skipping")
            return self._record_observability_and_build_result(
                trace_id,
                owner,
                repo,
                pr_number,
                start_time,
                run_handle,
                paths,
                [],
                0,
                [],
            )
        # Optionally post an initial "Viper has started a review" comment with an
        # auto-generated description when the PR lacks a useful description.
        if not dry_run:
            _maybe_post_started_review_comment(
                provider, owner, repo, pr_number, pr_info_for_metadata, paths
            )
        _, review_standards = self._detect_languages_for_files(paths)

        context_window = get_context_window()
        diff_budget = int(context_window * DIFF_TOKEN_BUDGET_RATIO)
        # Reserve room for diff/tool output and keep supplement bounded for both modes.
        remaining_prompt_tokens = max(0, context_window - diff_budget)

        refs, context_brief, prompt_suffix = self._build_prompt_suffix(
            provider,
            cfg,
            ctx_cfg,
            app_cfg,
            owner,
            repo,
            pr_number,
            pr_info_for_metadata,
            full_diff,
            remaining_prompt_tokens,
        )
        if refs:
            logger.info("context_aware: extracted %d reference(s) from PR text", len(refs))
        if context_brief:
            logger.info("context_aware: distilled context attached to review prompt")

        use_file_by_file = _estimate_tokens(full_diff) > diff_budget
        use_embedded_file_diffs = bool(incremental_base_sha and use_file_by_file)
        diff_by_path = (
            {
                path: unified_diff_for_path(full_diff, path)
                for path in paths
            }
            if use_embedded_file_diffs
            else None
        )
        if use_file_by_file:
            if use_embedded_file_diffs:
                logger.info(
                    "Running agent on %d file(s) (embedded file-by-file, incremental scope)",
                    len(paths),
                )
            else:
                logger.info("Running agent on %d file(s) (file-by-file)", len(paths))
        else:
            logger.info("Running agent (single shot)")

        session_id, session_service, runner = self._create_agent_and_runner(
            provider,
            review_standards,
            owner,
            repo,
            pr_number,
            use_file_by_file=use_file_by_file,
            disable_tools=True if use_embedded_file_diffs else None,
            context_brief_attached=bool(context_brief and "<context>" in prompt_suffix),
        )

        all_findings = self._run_agent_and_collect_findings(
            runner,
            session_service,
            session_id,
            owner,
            repo,
            pr_number,
            head_sha,
            paths,
            use_file_by_file,
            full_diff=full_diff,
            prompt_suffix=prompt_suffix,
            diff_by_path=diff_by_path,
        )

        all_findings = self._filter_findings_by_diff_scope(all_findings, paths, full_diff)

        to_post = self._attach_fingerprints_and_filter_findings(
            all_findings,
            provider,
            owner,
            repo,
            head_sha,
            ignore_set,
            resolved_body_set,
            resolved_fp_set,
        )
        logger.info(
            "Agent returned %d finding(s), %d to post after filtering",
            len(all_findings),
            len(to_post),
        )

        self._print_findings_summary(print_findings, to_post)

        successful_post_count = self._post_findings_and_summary(
            provider,
            owner,
            repo,
            pr_number,
            head_sha,
            dry_run,
            to_post,
            cfg,
            llm_cfg,
            existing,
            full_diff=full_diff,
        )
        self._log_post_counts(dry_run, len(to_post), successful_post_count)

        return self._record_observability_and_build_result(
            trace_id,
            owner,
            repo,
            pr_number,
            start_time,
            run_handle,
            paths,
            all_findings,
            successful_post_count,
            to_post,
            context_brief_attached=bool(context_brief and "<context>" in prompt_suffix),
        )


def run_review(
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str = "",
    *,
    dry_run: bool = False,
    print_findings: bool = False,
    review_decision_enabled: bool | None = None,
    review_decision_high_threshold: int | None = None,
    review_decision_medium_threshold: int | None = None,
    review_decision_only: bool = False,
    event_context: ReviewDecisionEventContext | None = None,
) -> list[FindingV1]:
    """
    Run the code review agent (findings-only mode). Fetches existing comments,
    runs agent, parses findings, filters by ignore list, and posts via provider.
    Returns list of findings that were posted (or would be posted if dry_run).

    Optional review-decision kwargs apply only to this run (they do not mutate
    the process-global cached :func:`~code_review.config.get_scm_config` instance).

    When *review_decision_only* is True (or ``CODE_REVIEW_REVIEW_DECISION_ONLY`` is set),
    skips the agent, inline posting, and idempotency short-circuit; only recomputes the
    quality gate and submits a PR review decision when enabled in SCM config.

    *event_context* may be supplied programmatically; when omitted, non-empty
    ``CODE_REVIEW_EVENT_*`` environment variables are parsed into
    :class:`~code_review.schemas.review_decision_event.ReviewDecisionEventContext`
    (used for review-decision-only logging and head SHA hints).
    """
    resolved_event = event_context or review_decision_event_context_from_env()
    orchestrator = ReviewOrchestrator(
        owner,
        repo,
        pr_number,
        head_sha,
        dry_run=dry_run,
        print_findings=print_findings,
        review_decision_enabled=review_decision_enabled,
        review_decision_high_threshold=review_decision_high_threshold,
        review_decision_medium_threshold=review_decision_medium_threshold,
        review_decision_only=review_decision_only,
        event_context=resolved_event,
    )
    return orchestrator.run()
