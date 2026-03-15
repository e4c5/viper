"""ADK Runner setup and programmatic invocation for code review."""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid

from google.genai import types
from litellm import AuthenticationError

import code_review
from code_review import observability
from code_review.agent import create_review_agent
from code_review.config import get_llm_config, get_scm_config
from code_review.diff.fingerprint import (
    build_fingerprint,
    format_comment_body_with_marker,
    parse_marker_from_comment_body,
    surrounding_content_hash,
)
from code_review.diff.parser import iter_new_lines, parse_unified_diff
from code_review.formatters.comment import finding_to_comment_body
from code_review.models import get_context_window
from code_review.providers import get_provider
from code_review.providers.base import InlineComment, RateLimitError
from code_review.schemas.findings import FindingV1
from code_review.standards import detect_from_paths, get_review_standards

APP_NAME = "code_review"
USER_ID = "reviewer"
AGENT_VERSION = getattr(code_review, "__version__", "0.1.0")
logger = logging.getLogger(__name__)

# Suppress expected "non-text parts" warning from google-genai when the model returns
# tool/function-call parts; we only use text parts in _collect_response_async.
def _filter_non_text_parts_warning(record: logging.LogRecord) -> bool:
    msg = record.getMessage()
    return "non-text parts" not in msg


_genai_logger = logging.getLogger("google_genai.types")
_genai_logger.addFilter(_filter_non_text_parts_warning)

# Fraction of context window reserved for diff content; rest for system prompt, tools, response.
# Configurable via LLM_DIFF_BUDGET_RATIO env var.
try:
    DIFF_TOKEN_BUDGET_RATIO = float(os.getenv("LLM_DIFF_BUDGET_RATIO", "0.25"))
except ValueError:
    DIFF_TOKEN_BUDGET_RATIO = 0.25


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (chars / 4) for diff and context budget checks."""
    return max(0, len(text) // 4)


def _normalize_path_for_anchor(file_path: str) -> str:
    """Normalize path like Bitbucket provider (strip dst://, src://, a/, b/) for diff line matching."""
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


def _generate_auto_pr_description(
    title: str, paths: list[str], max_files: int = 10
) -> str:
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
    return _post_comments_one_by_one(
        provider, owner, repo, pr_number, head_sha, comments
    )


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


def _post_run_marker_comment(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    cfg,
    llm_cfg,
    head_sha: str,
) -> None:
    """Post a minimal PR-level comment containing the run idempotency marker.

    Called after every non-dry run for providers that do not embed the fingerprint
    marker in inline comment bodies (omit_fingerprint_marker_in_body=True, e.g.
    Bitbucket Server).  Without this comment the run_id is never stored anywhere,
    so _idempotency_key_seen_in_comments can never fire and the runner re-processes
    the same PR on every CI trigger — even when all inline comments fail with 409.

    A PR-level comment (no anchor) is used so there are no lineType constraints
    that could cause 409 errors.
    """
    run_id = _build_idempotency_key(cfg, llm_cfg, owner, repo, pr_number, head_sha)
    body = format_comment_body_with_marker("", "", AGENT_VERSION, run_id=run_id)
    try:
        provider.post_pr_summary_comment(owner, repo, pr_number, body)
    except Exception as e:
        logger.warning(
            "_post_run_marker_comment failed owner=%s repo=%s pr_number=%s: %s",
            owner,
            repo,
            pr_number,
            e,
        )


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


async def _collect_response_async(
    runner, session_service, session_id: str, content: types.Content
) -> str:
    """Run agent once via run_async and return concatenated final response text.

    When CODE_REVIEW_LOG_LEVEL=DEBUG, log the raw final text we received from the LLM.
    """
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
    ):
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.head_sha = head_sha
        self.dry_run = dry_run
        self.print_findings = print_findings

    def _load_config_and_provider(self):
        """Load SCM/LLM config and create the provider instance.

        Returns (cfg, llm_cfg, provider).
        """
        cfg = get_scm_config()
        llm_cfg = get_llm_config()
        token_val = (
            cfg.token.get_secret_value() if hasattr(cfg.token, "get_secret_value") else cfg.token
        )
        provider = get_provider(cfg.provider, cfg.url, token_val)
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
            and any(
                lb.strip().lower() == cfg.skip_label.strip().lower() for lb in pr_info.labels
            )
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
        observability.finish_run(
            run_handle, owner, repo, pr_number, 0, 0, 0, _duration_ms / 1000.0
        )
        return []

    def _fetch_pr_files_and_diffs(self, provider, owner: str, repo: str, pr_number: int):
        """Fetch PR file list and full diff from the provider. Returns (files, paths, full_diff)."""
        files = provider.get_pr_files(owner, repo, pr_number)
        paths = [f.path for f in files]
        full_diff = provider.get_pr_diff(owner, repo, pr_number)
        return (files, paths, full_diff)

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
    ):
        """
        Build the findings-only agent, session service, and ADK Runner.
        Returns (session_id, session_service, runner).

        IMPORTANT: this method must be called AFTER determining use_file_by_file,
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
        """
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        agent = create_review_agent(
            provider,
            review_standards,
            findings_only=True,
            disable_tools=not use_file_by_file,
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
    ) -> list[FindingV1]:
        """
        Run the agent (file-by-file or single shot), parse response into FindingV1 list.
        Returns all_findings (unfiltered).

        When CODE_REVIEW_LOG_LEVEL=DEBUG, log exactly what we send to the LLM
        (the user message text) and the raw text we receive back, before any
        JSON parsing or filtering.
        """
        if use_file_by_file and paths:
            return self._run_file_by_file_mode(
                runner,
                session_service,
                owner,
                repo,
                pr_number,
                head_sha,
                paths,
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
        )

    def _run_file_by_file_mode(
        self,
        runner,
        session_service,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        paths: list[str],
    ) -> list[FindingV1]:
        all_findings: list[FindingV1] = []
        for file_path in paths:
            file_session_id = f"{owner}/{repo}/pr-{pr_number}/file/{uuid.uuid4().hex[:12]}"
            head_sha_clause = f" head_sha={head_sha}." if head_sha else ""
            ref_guidance = (
                f' When calling get_file_lines for surrounding context, use ref="{head_sha}" as the ref parameter.'
                if head_sha
                else ""
            )
            msg = (
                f"Review exactly one file from this PR. owner={owner}, repo={repo}, pr_number={pr_number}."
                + head_sha_clause
                + f' Call get_pr_diff_for_file(owner, repo, pr_number, "{file_path}") to get the diff for this file.'
                + ref_guidance
                + f' Then output a JSON array of findings for this file only. Use path "{file_path}" in every finding. '
                "If there are no issues in this file, output exactly []."
            )
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
    ) -> list[FindingV1]:
        # Single-shot mode: include the unified diff directly in the prompt so the
        # LLM can review the changes even if tool calls are unavailable or flaky.
        # We only reach this branch when the diff fits within the configured
        # DIFF_TOKEN_BUDGET_RATIO, so including the full diff is safe.
        msg = f"Review this PR: owner={owner}, repo={repo}, pr_number={pr_number}." + (
            f" head_sha={head_sha}." if head_sha else ""
        )
        if full_diff:
            msg += (
                "\n\nHere is the unified diff for this PR:\n"
                "```diff\n"
                f"{full_diff}\n"
                "```"
            )
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
            fp = (
                _fingerprint_for_finding(f, file_lines_by_path)
                if file_lines_by_path
                else ""
            )
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
        full_diff: used to set line_type (ADDED vs CONTEXT) so Bitbucket Server anchors comments correctly.
        """
        _resolve_stale_comments_if_supported(
            provider, owner, repo, pr_number, existing, to_post, head_sha, dry_run
        )
        if dry_run:
            return 0
        count = 0
        if to_post:
            if not head_sha:
                raise ValueError(
                    "head_sha is required when posting comments (dry_run=False). "
                    "Provide head_sha or use --dry-run to skip posting."
                )
            count = _post_inline_comments(
                provider, owner, repo, pr_number, head_sha, to_post, cfg, llm_cfg, full_diff=full_diff
            )
        # For providers that omit the fingerprint marker from inline comment bodies
        # (e.g. Bitbucket Server), there is no run_id persisted when inline posting
        # completely fails. In that specific case, post a dedicated PR-level marker
        # comment so future runs can short-circuit instead of infinite re-processing.
        #
        # When at least one inline comment is posted successfully, avoid posting this
        # marker comment because it appears as a visible, out-of-place activity entry.
        if (
            head_sha
            and to_post
            and count == 0
            and provider.capabilities().omit_fingerprint_marker_in_body
        ):
            _post_run_marker_comment(provider, owner, repo, pr_number, cfg, llm_cfg, head_sha)
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
        )
        return [f for f, _ in to_post]

    @staticmethod
    def _print_findings_summary(
        print_findings: bool, to_post: list[tuple[FindingV1, str]]
    ) -> None:
        if not print_findings:
            return
        if to_post:
            for f, _ in to_post:
                print(f"{f.path}:{f.line} [{f.severity}] {f.get_body()}")
        else:
            print("No findings to post.")

    @staticmethod
    def _log_post_counts(
        dry_run: bool, planned_count: int, successful_post_count: int
    ) -> None:
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
        # Paths may include prefixes like dst:// or src:// (especially for Bitbucket);
        # normalize both diff paths and finding paths before comparison so valid findings
        # are not dropped just because of a prefix difference.
        path_filtered = self._filter_findings_to_pr_paths(findings, paths)

        # Guardrail: only keep findings for lines that are actually visible in the diff.
        # Comments on lines outside diff hunks cannot be placed inline: Bitbucket Cloud
        # rejects them (causing fallback to PR-level activity comments) and GitHub/Gitea
        # reject them via position-based APIs. In single-shot mode the LLM receives the
        # full multi-file diff and may report lines that are in the file but outside any
        # hunk (e.g. unchanged code far from the changed region). Filtering here ensures
        # every posted comment appears inline in the diff view.
        return self._filter_findings_to_visible_diff_lines(path_filtered, full_diff)

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
        logger.info(
            "Reviewing %s/%s PR %s (provider=%s)",
            owner,
            repo,
            pr_number,
            cfg.provider,
        )

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

        pr_info_for_metadata = provider.get_pr_info(owner, repo, pr_number)

        _, paths, full_diff = self._fetch_pr_files_and_diffs(provider, owner, repo, pr_number)
        paths = self._build_ignore_set_and_filter_files(paths)
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

        diff_budget = int(get_context_window() * DIFF_TOKEN_BUDGET_RATIO)
        use_file_by_file = _estimate_tokens(full_diff) > diff_budget
        if use_file_by_file:
            logger.info("Running agent on %d file(s) (file-by-file)", len(paths))
        else:
            logger.info("Running agent (single shot)")

        session_id, session_service, runner = self._create_agent_and_runner(
            provider, review_standards, owner, repo, pr_number,
            use_file_by_file=use_file_by_file
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
        )


def run_review(
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str = "",
    *,
    dry_run: bool = False,
    print_findings: bool = False,
) -> list[FindingV1]:
    """
    Run the code review agent (findings-only mode). Fetches existing comments,
    runs agent, parses findings, filters by ignore list, and posts via provider.
    Returns list of findings that were posted (or would be posted if dry_run).
    """
    orchestrator = ReviewOrchestrator(
        owner, repo, pr_number, head_sha, dry_run=dry_run, print_findings=print_findings
    )
    return orchestrator.run()
