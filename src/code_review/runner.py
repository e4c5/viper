"""ADK Runner setup and programmatic invocation for code review."""

import hashlib
import json
import logging
import re
import time
import uuid

from google.genai import types

import code_review
from code_review.agent import create_review_agent
from code_review.config import get_scm_config, get_llm_config
from code_review.diff.fingerprint import (
    build_fingerprint,
    format_comment_body_with_marker,
    parse_marker_from_comment_body,
    surrounding_content_hash,
)
from code_review.formatters.comment import finding_to_comment_body
from code_review.models import get_context_window
from code_review import observability
from code_review.providers import get_provider
from code_review.providers.base import InlineComment
from code_review.schemas.findings import FindingV1
from code_review.standards import detect_from_paths, get_review_standards

APP_NAME = "code_review"
USER_ID = "reviewer"
AGENT_VERSION = getattr(code_review, "__version__", "0.1.0")
logger = logging.getLogger(__name__)

# Fraction of context window reserved for diff content; rest for system prompt, tools, response
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
    return f"{scm_cfg.provider}/{owner}/{repo}/pr/{pr_number}/head/{head_sha}/agent/{AGENT_VERSION}/config/{config_hash}"


def _idempotency_key_seen_in_comments(comments: list, key: str) -> bool:
    """Return True if any comment body contains run=<key> in code-review-agent marker."""
    for c in comments:
        body = getattr(c, "body", None) or (c.get("body") if isinstance(c, dict) else "")
        if body:
            parsed = parse_marker_from_comment_body(body)
            if parsed.get("run") == key:
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


def _get_file_lines_by_path(provider, owner: str, repo: str, ref: str, paths: list[str]) -> dict[str, list[str]]:
    """Fetch file content at ref for each path; return dict path -> list of lines."""
    out: dict[str, list[str]] = {}
    for p in paths:
        try:
            content = provider.get_file_content(owner, repo, ref, p)
            out[p] = content.splitlines()
        except Exception as e:
            logger.warning(
                "get_file_content failed for path=%s owner=%s repo=%s ref=%s: %s",
                p, owner, repo, ref, e,
                exc_info=True,
            )
            out[p] = []
    return out


def _build_pr_summary_body(to_post: list[tuple[FindingV1, str]]) -> str:
    """Build PR-level summary: counts by severity and link to inline comments (Phase 4.2)."""
    from collections import Counter
    counts = Counter(f.severity for f, _ in to_post)
    parts = [f"{count} {str(sev).capitalize()}" for sev, count in sorted(counts.items())]
    summary = "Code review: " + ", ".join(parts) + "."
    return summary + "\n\nSee inline comments above."


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


def _run_agent_and_collect_response(
    runner, session_id: str, content: types.Content
) -> str:
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
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    trace_id = str(uuid.uuid4())
    start_time = time.perf_counter()
    run_handle = observability.start_run(trace_id)

    cfg = get_scm_config()
    llm_cfg = get_llm_config()
    provider = get_provider(cfg.provider, cfg.url, cfg.token)

    # Skip review if PR has skip label or title contains skip pattern (e.g. [skip-review])
    if cfg.skip_label or cfg.skip_title_pattern:
        pr_info = provider.get_pr_info(owner, repo, pr_number)
        if pr_info:
            if cfg.skip_label and cfg.skip_label.strip() and any(
                lb.strip().lower() == cfg.skip_label.strip().lower()
                for lb in pr_info.labels
            ):
                _duration_ms = (time.perf_counter() - start_time) * 1000
                _log_run_complete(
                    trace_id, owner, repo, pr_number, 0, 0, 0, _duration_ms
                )
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
                _log_run_complete(
                    trace_id, owner, repo, pr_number, 0, 0, 0, _duration_ms
                )
                observability.finish_run(
                    run_handle, owner, repo, pr_number, 0, 0, 0, _duration_ms / 1000.0
                )
                return []

    # Runner fetches existing comments and builds ignore list
    existing = provider.get_existing_review_comments(owner, repo, pr_number)
    existing_dicts = [c.model_dump() for c in existing]
    ignore_set = _build_ignore_set(existing_dicts)

    # Idempotency: skip if we already ran for this PR/head/config (run id in comment marker)
    if head_sha:
        run_id = _build_idempotency_key(
            cfg, llm_cfg, owner, repo, pr_number, head_sha
        )
        if _idempotency_key_seen_in_comments(existing_dicts, run_id):
            _duration_ms = (time.perf_counter() - start_time) * 1000
            _log_run_complete(
                trace_id, owner, repo, pr_number, 0, 0, 0, _duration_ms
            )
            observability.finish_run(
                run_handle, owner, repo, pr_number, 0, 0, 0, _duration_ms / 1000.0
            )
            return []

    files = provider.get_pr_files(owner, repo, pr_number)
    paths = [f.path for f in files]
    detected = detect_from_paths(paths)
    review_standards = get_review_standards(detected.language, detected.framework)

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

    diff_budget = int(get_context_window() * DIFF_TOKEN_BUDGET_RATIO)
    full_diff = provider.get_pr_diff(owner, repo, pr_number)
    use_file_by_file = _estimate_tokens(full_diff) > diff_budget

    all_findings: list[FindingV1] = []
    if use_file_by_file and paths:
        for file_path in paths:
            # Create a fresh session per file: avoids accumulating prior-file context
            # in the ADK session history, which would grow the context window linearly
            # and waste tokens across files.
            file_session_id = f"{owner}/{repo}/pr-{pr_number}/file/{uuid.uuid4().hex[:12]}"
            session_service.create_session_sync(
                app_name=APP_NAME,
                user_id=USER_ID,
                session_id=file_session_id,
            )
            msg = (
                f"Review this PR: owner={owner}, repo={repo}, pr_number={pr_number}."
                + (f" head_sha={head_sha}." if head_sha else "")
                + f" Review only this file: {file_path}. Use get_pr_diff_for_file to fetch its diff."
            )
            content = types.Content(role="user", parts=[types.Part(text=msg)])
            response_text = _run_agent_and_collect_response(
                runner, file_session_id, content
            )
            all_findings.extend(_findings_from_response(response_text))
    else:
        msg = (
            f"Review this PR: owner={owner}, repo={repo}, pr_number={pr_number}."
            + (f" head_sha={head_sha}." if head_sha else "")
        )
        content = types.Content(role="user", parts=[types.Part(text=msg)])
        response_text = _run_agent_and_collect_response(
            runner, session_id, content
        )
        all_findings = _findings_from_response(response_text)

    # Filter out findings that match existing comments (by path + body_hash or path + fingerprint)
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
        if (f.path, body_hash) in ignore_set:
            continue
        if file_lines_by_path:
            fp = _fingerprint_for_finding(f, file_lines_by_path)
            if (f.path, fp) in ignore_set:
                continue
            ignore_set.add((f.path, fp))
        else:
            fp = ""
        ignore_set.add((f.path, body_hash))
        to_post.append((f, fp))

    if print_findings:
        for f, _ in to_post:
            print(f"{f.path}:{f.line} [{f.severity}] {f.get_body()}")

    successful_post_count = 0
    if not dry_run and to_post:
        if not head_sha:
            raise ValueError(
                "head_sha is required when posting comments (dry_run=False). "
                "Provide head_sha or use --dry-run to skip posting."
            )
        run_id = _build_idempotency_key(
            cfg, llm_cfg, owner, repo, pr_number, head_sha
        )
        comments: list[InlineComment] = []
        for f, fp in to_post:
            body = finding_to_comment_body(f)
            if fp:
                body = format_comment_body_with_marker(
                    body, fp, AGENT_VERSION, run_id=run_id
                )
            comments.append(
                InlineComment(path=f.path, line=f.line, body=body, end_line=f.end_line)
            )
        try:
            provider.post_review_comments(
                owner, repo, pr_number, comments, head_sha=head_sha
            )
            successful_post_count = len(comments)
            try:
                provider.post_pr_summary_comment(
                    owner, repo, pr_number, _build_pr_summary_body(to_post)
                )
            except Exception as e:
                logger.warning(
                    "post_pr_summary_comment failed owner=%s repo=%s pr_number=%s: %s",
                    owner, repo, pr_number, e,
                )
        except Exception:
            # Batch failed (e.g. one position invalid); post one-by-one, degrade to PR-level on failure
            for c in comments:
                try:
                    provider.post_review_comment(
                        owner, repo, pr_number, c.path, c.line, c.body, head_sha=head_sha
                    )
                    successful_post_count += 1
                except Exception:
                    summary_body = f"**{c.path}:{c.line}**\n\n{c.body}"
                    try:
                        provider.post_pr_summary_comment(
                            owner, repo, pr_number, summary_body
                        )
                        successful_post_count += 1
                    except Exception as e:
                        logger.error(
                            "post_pr_summary_comment failed owner=%s repo=%s pr_number=%s path=%s line=%s: %s",
                            owner, repo, pr_number, c.path, c.line, e,
                            exc_info=True,
                        )

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
