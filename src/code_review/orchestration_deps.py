"""Shared orchestration helpers for runner and ReviewOrchestrator."""

from __future__ import annotations

import hashlib
import logging
import os
import time  # noqa: F401
import uuid  # noqa: F401
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

import code_review
from code_review import observability  # noqa: F401
from code_review.agent import (
    create_review_agent,  # noqa: F401
    reply_dismissal_verdict_from_llm_text,  # noqa: F401
)
from code_review.config import (
    get_code_review_app_config,  # noqa: F401
    get_context_aware_config,  # noqa: F401
    get_llm_config,  # noqa: F401
    get_scm_config,  # noqa: F401
)
from code_review.context.errors import ContextAwareFatalError  # noqa: F401
from code_review.context.extract import extract_context_references  # noqa: F401
from code_review.context.pipeline import build_context_brief_for_pr  # noqa: F401
from code_review.context.validation import validate_context_aware_sources  # noqa: F401
from code_review.diff.fingerprint import (
    build_fingerprint,
    surrounding_content_hash,
)
from code_review.diff.line_index import (
    build_diff_line_index as _build_diff_line_index,  # noqa: F401
)
from code_review.diff.line_index import (
    build_per_file_line_index as _build_per_file_line_index,  # noqa: F401
)
from code_review.diff.parser import (
    parse_unified_diff,
)
from code_review.diff.utils import estimate_tokens as _estimate_tokens  # noqa: F401
from code_review.diff.utils import normalize_path as _normalize_path_for_anchor  # noqa: F401
from code_review.models import (
    PRContext,
    get_context_window,  # noqa: F401
    get_max_output_tokens,  # noqa: F401
)
from code_review.providers import get_provider  # noqa: F401
from code_review.providers.base import (
    RateLimitError,  # noqa: F401
    )
from code_review.refinement.filters.anchor_relocator import (
    _ANCHOR_RELOCATION_WINDOW,  # noqa: F401
    _find_closest_anchor_line,  # noqa: F401
    _maybe_relocate_finding,  # noqa: F401
)
from code_review.refinement.filters.anchor_relocator import (
    relocate_findings_by_anchor as _relocate_findings_by_anchor,  # noqa: F401
)
from code_review.refinement.filters.contradiction import (
    _message_describes_syntax_or_missing_token_issue,  # noqa: F401
)
from code_review.refinement.filters.contradiction import (
    filter_obviously_contradicted_findings as _filter_obviously_contradicted_findings,  # noqa: F401
)
from code_review.refinement.filters.patch_validator import (
    validate_suggested_patches as _validate_suggested_patches,  # noqa: F401
)
from code_review.refinement.filters.self_retraction import (
    _finding_message_looks_self_retracted,  # noqa: F401
)
from code_review.refinement.filters.self_retraction import (
    filter_self_retracted_findings as _filter_self_retracted_finding_messages,  # noqa: F401
)
from code_review.reply_dismissal_state import REPLY_DISMISSAL_ACCEPTED_REPLY_TEXT  # noqa: F401
from code_review.schemas.findings import FindingV1
from code_review.schemas.reply_dismissal import ReplyDismissalVerdictV1  # noqa: F401
from code_review.schemas.review_decision_event import (
    ReviewDecisionEventContext,
    event_allows_decision_only_skip_when_bot_not_blocking,  # noqa: F401
)
from code_review.standards.detector import detect_from_paths  # noqa: F401
from code_review.standards.prompts import get_review_standards  # noqa: F401

APP_NAME = "code_review"
USER_ID = "reviewer"
AGENT_VERSION = getattr(code_review, "__version__", "0.1.0")
logger = logging.getLogger(__name__)

# Fraction of context window reserved for diff content; rest for system prompt and response.
# Configurable via LLM_DIFF_BUDGET_RATIO env var.
try:
    DIFF_TOKEN_BUDGET_RATIO = float(os.getenv("LLM_DIFF_BUDGET_RATIO", "0.5"))
except ValueError:
    DIFF_TOKEN_BUDGET_RATIO = 0.5

# ---------------------------------------------------------------------------
# Re-exports from focused submodules (canonical implementations live there).
# ---------------------------------------------------------------------------
from code_review.orchestration.events import (  # noqa: E402
    ReplyDismissalContext,
)
from code_review.context.errors import ContextAwareFatalError  # noqa: E402,F401
from code_review.diff.utils import normalize_path as _normalize_path_for_anchor  # noqa: E402,F401
from code_review.orchestration.events import (  # noqa: E402
    _reply_added_event_authored_by_bot,  # noqa: F401
)
from code_review.orchestration.idempotency import (  # noqa: E402
    _idempotency_key_seen_in_comments,  # noqa: F401
)
from code_review.orchestration.posting import (  # noqa: E402
    CommentPoster,
    _added_lines_in_diff,  # noqa: F401
    _generate_auto_pr_description,  # noqa: F401
    _omit_marker_pr_summary_visible_text,  # noqa: F401
)
from code_review.orchestration.prompts import (  # noqa: E402
    _build_commit_messages_block,  # noqa: F401
    _format_review_prompt_supplement,  # noqa: F401
)
from code_review.orchestration.runner_utils import (  # noqa: E402
    APP_NAME,  # noqa: F401
    PartialResponseCollectionError,  # noqa: F401
    _bypass_adk_templating,  # noqa: F401
    _findings_from_response,  # noqa: F401
    _get_output_key_findings,  # noqa: F401
    _log_run_complete,  # noqa: F401
    _parse_findings_json,  # noqa: F401
    _run_agent_and_collect_response,  # noqa: F401
    _run_agent_and_collect_responses,  # noqa: F401
    _run_reply_dismissal_llm,  # noqa: F401
    _suppress_ssl_teardown_errors,  # noqa: F401
)
from code_review.reply_dismissal_state import (  # noqa: E402
    REPLY_DISMISSAL_ACCEPTED_REPLY_TEXT,  # noqa: F401
)
from code_review.agent.reply_dismissal_agent import (  # noqa: E402
    reply_dismissal_verdict_from_llm_text,  # noqa: F401
)
from code_review.config import (  # noqa: E402
    get_code_review_app_config,  # noqa: F401
    get_context_aware_config,  # noqa: F401
    get_llm_config,  # noqa: F401
    get_scm_config,  # noqa: F401
)
from code_review.context.extract import extract_context_references  # noqa: E402,F401
from code_review.context.pipeline import build_context_brief_for_pr  # noqa: E402,F401
from code_review.context.validation import validate_context_aware_sources  # noqa: E402,F401
from code_review.models import (  # noqa: E402
    get_context_window,  # noqa: F401
    get_max_output_tokens,  # noqa: F401
)
from code_review.providers import get_provider  # noqa: E402,F401
from code_review.providers.base import (  # noqa: E402
    RateLimitError,  # noqa: F401
    unified_diff_for_path,  # noqa: F401
)
from code_review.schemas.findings import FindingV1  # noqa: E402,F401
from code_review.schemas.review_decision_event import (  # noqa: E402
    event_allows_decision_only_skip_when_bot_not_blocking,  # noqa: F401
)
from code_review.standards.detector import detect_from_paths  # noqa: E402,F401
from code_review.standards.prompts import get_review_standards  # noqa: E402,F401
from google.genai import types  # noqa: E402,F401


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


def _maybe_post_started_review_comment(
    provider,
    pr_ctx: PRContext,
    pr_info,
    paths: list[str],
) -> None:
    """Compatibility shim — delegates to CommentPoster."""
    CommentPoster(provider, pr_ctx).post_started_review_comment(pr_info, paths)


def _resolve_stale_comments_if_supported(
    provider,
    pr_ctx: PRContext,
    existing: list,
    to_post: list[tuple[FindingV1, str]],
    dry_run: bool,
) -> None:
    """Compatibility shim — delegates to CommentPoster."""
    CommentPoster(provider, pr_ctx).resolve_stale(existing, to_post, dry_run)


def _post_inline_comments(
    provider,
    pr_ctx: PRContext,
    incremental_base_sha: str,
    to_post: list[tuple[FindingV1, str]],
    cfg,
    llm_cfg,
    full_diff: str = "",
) -> int:
    """Compatibility shim — delegates to CommentPoster."""
    return CommentPoster(provider, pr_ctx).post_inline(
        incremental_base_sha, to_post, cfg, llm_cfg, full_diff=full_diff
    )


def _post_comments_one_by_one(
    provider,
    pr_ctx: PRContext,
    comments: list,
) -> int:
    """Compatibility shim — delegates to CommentPoster._post_comments_one_by_one."""
    return CommentPoster(provider, pr_ctx)._post_comments_one_by_one(comments)


def _post_omit_marker_pr_summary_comment(
    provider,
    pr_ctx: PRContext,
    cfg,
    llm_cfg,
    incremental_base_sha: str = "",
    *,
    findings_planned: int,
    successful_inline_posts: int,
    gate_outcome: QualityGateReviewOutcome,
    include_run_marker: bool = True,
) -> None:
    """Compatibility shim — delegates to CommentPoster."""
    CommentPoster(provider, pr_ctx).post_omit_marker_summary(
        cfg,
        llm_cfg,
        incremental_base_sha,
        findings_planned=findings_planned,
        successful_inline_posts=successful_inline_posts,
        gate_outcome=gate_outcome,
        include_run_marker=include_run_marker,
    )


from code_review.quality.gate import (  # noqa: E402
    _compute_quality_gate_review_outcome,  # noqa: F401
    _log_quality_gate_review_outcome,  # noqa: F401
)
from code_review.quality.outcome import (  # noqa: E402
    QualityGateReviewOutcome,  # noqa: F401
)

# PartialResponseCollectionError, _parse_findings_json, _findings_from_response,
# _log_run_complete, _suppress_ssl_teardown_errors, _run_agent_and_collect_response,
# _bypass_adk_templating, _run_agent_and_collect_responses, _run_reply_dismissal_llm
# are all re-exported from orchestration.runner_utils above.


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
    gate_outcome: QualityGateReviewOutcome | None,
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
    if gate_outcome is None:
        logger.warning(
            "Skipping PR review decision submission: quality gate could not be computed "
            "because unresolved review-item lookup failed."
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


# _normalize_scm_identity_fragment, _event_actor_matches_*, _reply_added_event_authored_by_bot,
# _reply_dismissal_entry_*, _reply_dismissal_{original,existing,scm,diff}_*, ReplyDismissalContext
# are all re-exported from orchestration.events above.

# _format_reply_dismissal_user_message is aliased below for backward compatibility.
def _format_reply_dismissal_user_message(
    ctx,
    bot,
    triggering_comment_id: str,
    diff_context: str = "",
) -> str:
    """Compatibility shim — delegates to ReplyDismissalContext.format_user_message."""
    return ReplyDismissalContext(ctx, bot).format_user_message(triggering_comment_id, diff_context)


__all__ = [
    name
    for name in globals()
    if name != "__all__" and not (name.startswith("__") and name.endswith("__"))
]
