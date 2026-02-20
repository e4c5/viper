"""ADK Runner setup and programmatic invocation for code review."""

import hashlib
import json
import re
import uuid

from google.genai import types

from code_review.agent import create_review_agent
from code_review.config import get_scm_config
from code_review.models import get_context_window
from code_review.providers import get_provider
from code_review.schemas.findings import FindingV1
from code_review.standards import detect_from_paths, get_review_standards

APP_NAME = "code_review"
USER_ID = "reviewer"

# Fraction of context window reserved for diff content; rest for system prompt, tools, response
DIFF_TOKEN_BUDGET_RATIO = 0.25


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (chars / 4) for diff and context budget checks."""
    return max(0, len(text) // 4)


def _build_ignore_set(comments: list) -> set[tuple[str, str]]:
    """Build set of (path, body_hash) from existing review comments for dedup."""
    out: set[tuple[str, str]] = set()
    for c in comments:
        path = getattr(c, "path", None) or (c.get("path") if isinstance(c, dict) else "")
        body = getattr(c, "body", None) or (c.get("body") if isinstance(c, dict) else "")
        if path and body:
            out.add((path, hashlib.sha256(body.encode()).hexdigest()))
    return out


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


def _finding_to_comment_body(f: FindingV1) -> str:
    """Format finding as inline comment body with severity prefix."""
    severity_label = f"[{f.severity.title()}]"
    return f"{severity_label} {f.get_body()}"


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

    cfg = get_scm_config()
    provider = get_provider(cfg.provider, cfg.url, cfg.token)

    # Runner fetches existing comments and builds ignore list
    existing = provider.get_existing_review_comments(owner, repo, pr_number)
    ignore_set = _build_ignore_set([c.model_dump() for c in existing])

    files = provider.get_pr_files(owner, repo, pr_number)
    paths = [f.path for f in files]
    detected = detect_from_paths(paths)
    review_standards = get_review_standards(detected.language, detected.framework)

    agent = create_review_agent(provider, review_standards, findings_only=True)

    session_id = f"{owner}_{repo}_{pr_number}_{uuid.uuid4().hex[:12]}"
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
            msg = (
                f"Review this PR: owner={owner}, repo={repo}, pr_number={pr_number}."
                + (f" head_sha={head_sha}." if head_sha else "")
                + f" Review only this file: {file_path}."
            )
            content = types.Content(role="user", parts=[types.Part(text=msg)])
            response_text = _run_agent_and_collect_response(
                runner, session_id, content
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

    # Filter out findings that match existing comments (by path + body hash)
    to_post: list[FindingV1] = []
    for f in all_findings:
        body = _finding_to_comment_body(f)
        key = (f.path, hashlib.sha256(body.encode()).hexdigest())
        if key not in ignore_set:
            to_post.append(f)
            ignore_set.add(key)

    if print_findings:
        for f in to_post:
            print(f"{f.path}:{f.line} [{f.severity}] {f.get_body()}")

    if not dry_run and to_post:
        comments = [
            (f.path, f.line, _finding_to_comment_body(f)) for f in to_post
        ]
        provider.post_review_comments(
            owner, repo, pr_number, comments, head_sha=head_sha
        )

    return to_post
