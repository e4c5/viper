"""ADK Runner setup and programmatic invocation for code review."""

import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import Counter

from google.genai import types

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
from code_review.formatters.comment import finding_to_comment_body
from code_review.models import get_context_window
from code_review.providers import get_provider
from code_review.providers.base import InlineComment
from code_review.schemas.findings import FindingV1
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
    Build a simple, deterministic PR description when none was provided.

    Uses the title and the list of changed file paths so downstream tools
    (and humans) have a quick summary even if the author omitted one.
    """
    title_str = title.strip() or "Untitled change"
    unique_paths = list(dict.fromkeys(paths))
    shown_paths = unique_paths[:max_files]
    files_part = ", ".join(f"`{p}`" for p in shown_paths) if shown_paths else "no files detected"
    more_suffix = ""
    if len(unique_paths) > max_files:
        more_suffix = f", and {len(unique_paths) - max_files} more file(s)"
    return (
        f"**Title**: {title_str}\n\n"
        f"This pull request updates {len(unique_paths)} file(s): {files_part}{more_suffix}."
    )


def _maybe_post_started_review_comment(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    pr_info,
    paths: list[str],
) -> None:
    """
    If the PR has no meaningful description, post a PR-level comment indicating
    that Viper has started a review and include an auto-generated description.
    """
    if not pr_info:
        return
    if not paths:
        return
    description = (getattr(pr_info, "description", "") or "").strip()
    # Treat very short descriptions as effectively missing.
    if len(description) >= 40:
        return
    auto_desc = _generate_auto_pr_description(getattr(pr_info, "title", "") or "", paths)
    body = (
        "Viper has started a review of this pull request.\n\n"
        "The pull request did not include a detailed description, so the following "
        "summary was auto-generated:\n\n"
        f"{auto_desc}"
    )
    try:
        provider.post_pr_summary_comment(owner, repo, pr_number, body)
    except Exception as e:  # pragma: no cover - defensive; providers should implement this
        logger.warning(
            "post_pr_summary_comment (started review) failed owner=%s repo=%s pr_number=%s: %s",
            owner,
            repo,
            pr_number,
            e,
        )


def _build_pr_summary_body(to_post: list[tuple[FindingV1, str]]) -> str:
    """Build PR-level summary: counts by severity and link to inline comments (Phase 4.2)."""
    counts = Counter(f.severity for f, _ in to_post)
    parts = [f"{count} {str(sev).capitalize()}" for sev, count in sorted(counts.items())]
    summary = "Code review: " + ", ".join(parts) + "."
    return summary + "\n\nSee inline comments above."


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


def _post_inline_comments_with_fallback(
    provider,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    to_post: list[tuple[FindingV1, str]],
    cfg,
    llm_cfg,
) -> int:
    """Build inline comments, post batch (or per-comment fallback), then summary. Returns count."""
    run_id = _build_idempotency_key(cfg, llm_cfg, owner, repo, pr_number, head_sha)
    comments: list[InlineComment] = []
    for f, fp in to_post:
        body = finding_to_comment_body(f)
        if fp:
            body = format_comment_body_with_marker(
                body, fp, AGENT_VERSION, run_id=run_id
            )
        comments.append(
            InlineComment(
                path=f.path,
                line=f.line,
                body=body,
                end_line=f.end_line,
                suggested_patch=f.suggested_patch,
            )
        )
    try:
        provider.post_review_comments(
            owner, repo, pr_number, comments, head_sha=head_sha
        )
        count = len(comments)
        try:
            provider.post_pr_summary_comment(
                owner, repo, pr_number, _build_pr_summary_body(to_post)
            )
        except Exception as e:
            logger.warning(
                "post_pr_summary_comment failed owner=%s repo=%s pr_number=%s: %s",
                owner,
                repo,
                pr_number,
                e,
            )
        return count
    except Exception:
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
    """Post each comment individually; on failure post as PR summary. Returns count."""
    count = 0
    for c in comments:
        try:
            provider.post_review_comment(
                owner,
                repo,
                pr_number,
                c.path,
                c.line,
                c.body,
                end_line=c.end_line,
                suggested_patch=c.suggested_patch,
                head_sha=head_sha,
            )
            count += 1
        except Exception:
            summary_body = f"**{c.path}:{c.line}**\n\n{c.body}"
            try:
                provider.post_pr_summary_comment(
                    owner, repo, pr_number, summary_body
                )
                count += 1
            except Exception as e:
                logger.error(
                    "post_pr_summary_comment failed owner=%s repo=%s "
                    "pr_number=%s path=%s line=%s: %s",
                    owner,
                    repo,
                    pr_number,
                    c.path,
                    c.line,
                    e,
                    exc_info=True,
                )
    return count


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
    findings: list[FindingV1] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            # Normalize keys: anchor -> fingerprint_hint if needed
            if "anchor" in item and "fingerprint_hint" not in item:
                item = {**item, "fingerprint_hint": item.get("anchor")}
            findings.append(FindingV1.model_validate(item))
        except Exception:
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


def _run_agent_and_collect_response(runner, session_id: str, content: types.Content) -> str:
    """Run agent once and return concatenated final response text."""
    parts: list[str] = []
    for event in runner.run(
        user_id=USER_ID,
        session_id=session_id,
        new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    parts.append(part.text)
    return "\n".join(parts)


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
        self, provider, review_standards: str, owner: str, repo: str, pr_number: int
    ):
        """
        Build the findings-only agent, session service, and ADK Runner.
        Returns (session_id, session_service, runner).
        """
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        agent = create_review_agent(provider, review_standards, findings_only=True)
        session_id = f"{owner}/{repo}/pr-{pr_number}/{uuid.uuid4().hex[:12]}"
        session_service = InMemorySessionService()
        session_service.create_session_sync(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
        )
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
    ) -> list[FindingV1]:
        """
        Run the agent (file-by-file or single shot), parse response into FindingV1 list.
        Returns all_findings (unfiltered).
        """
        all_findings: list[FindingV1] = []
        if use_file_by_file and paths:
            for file_path in paths:
                file_session_id = f"{owner}/{repo}/pr-{pr_number}/file/{uuid.uuid4().hex[:12]}"
                session_service.create_session_sync(
                    app_name=APP_NAME,
                    user_id=USER_ID,
                    session_id=file_session_id,
                )
                msg = (
                    f"Review this PR: owner={owner}, repo={repo}, pr_number={pr_number}."
                    + (f" head_sha={head_sha}." if head_sha else "")
                    + " Review only this file: "
                    f"{file_path}. Use get_pr_diff_for_file to fetch its diff."
                )
                content = types.Content(role="user", parts=[types.Part(text=msg)])
                response_text = _run_agent_and_collect_response(
                    runner, file_session_id, content
                )
                all_findings.extend(_findings_from_response(response_text))
        else:
            msg = f"Review this PR: owner={owner}, repo={repo}, pr_number={pr_number}." + (
                f" head_sha={head_sha}." if head_sha else ""
            )
            content = types.Content(role="user", parts=[types.Part(text=msg)])
            response_text = _run_agent_and_collect_response(runner, session_id, content)
            all_findings = _findings_from_response(response_text)
        return all_findings

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
    ) -> int:
        """
        Auto-resolve stale comments (if supported), then post inline comments and PR summary.
        Returns successful_post_count.
        """
        _resolve_stale_comments_if_supported(
            provider, owner, repo, pr_number, existing, to_post, head_sha, dry_run
        )
        if not dry_run and to_post:
            if not head_sha:
                raise ValueError(
                    "head_sha is required when posting comments (dry_run=False). "
                    "Provide head_sha or use --dry-run to skip posting."
                )
            return _post_inline_comments_with_fallback(
                provider, owner, repo, pr_number, head_sha, to_post, cfg, llm_cfg
            )
        return 0

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
            return idempotency_result

        pr_info_for_metadata = provider.get_pr_info(owner, repo, pr_number)

        files, paths, full_diff = self._fetch_pr_files_and_diffs(provider, owner, repo, pr_number)
        paths = self._build_ignore_set_and_filter_files(paths)
        if not paths:
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

        session_id, session_service, runner = self._create_agent_and_runner(
            provider, review_standards, owner, repo, pr_number
        )

        diff_budget = int(get_context_window() * DIFF_TOKEN_BUDGET_RATIO)
        use_file_by_file = _estimate_tokens(full_diff) > diff_budget

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
        )

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

        if print_findings:
            for f, _ in to_post:
                print(f"{f.path}:{f.line} [{f.severity}] {f.get_body()}")

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
        )

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
