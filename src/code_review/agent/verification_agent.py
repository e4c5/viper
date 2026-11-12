"""Verification agent: second-opinion LLM pass over findings before they are posted.

Every finding that is not labelled confidence="high" is sent for verification,
including findings with no confidence label. Batch-mode reviews tell the model not
to emit confidence, so an unset label is the normal case, not a sign of certainty.
Only findings the reviewer itself marked high-confidence bypass the check.

Each finding is shown with the nearby diff lines and, when the caller supplies a
file fetcher, a wider window of the file at the head commit plus the project's
root manifests, so the verifier can see callers, definitions and the targeted
language version that a few diff lines leave out.
"""

from __future__ import annotations

import json  # stdlib — no reason to defer
import logging
import uuid
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from code_review.json_utils import iter_json_candidates
from code_review.llm_telemetry import log_adk_llm_usage
from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

_MAX_FINDINGS_PER_BATCH = 20  # avoid token blow-up on large PRs
_FILE_CONTEXT_RADIUS = 40  # lines either side of the finding from the head-commit file
_FILE_CONTEXT_MAX_SPAN = 120  # cap on end_line - line, so a huge range cannot pull in a whole file
_MAX_MANIFEST_LINES = 60
_MAX_OUTPUT_TOKENS = 16384  # room for adaptive thinking before the verdict JSON

# Root files that state the language version or runtime a project targets.
PROJECT_MANIFESTS: tuple[str, ...] = (
    "pyproject.toml",
    "setup.cfg",
    "package.json",
    "tsconfig.json",
    "go.mod",
    "Cargo.toml",
)

# Returns a file's content at the reviewed head commit, or None when unavailable.
FileFetcher = Callable[[str], str | None]


class _VerificationVerdict(BaseModel):
    """Verdict for one finding in the verification batch."""

    index: int = Field(..., description="Zero-based index matching the input finding list")
    verdict: Literal["confirm", "reject"] = Field(
        ..., description="confirm if the concern is valid; reject if the code is fine"
    )
    reason: str = Field(..., description="One-sentence explanation of the decision")


class _VerificationResult(BaseModel):
    """Structured output for the verification agent."""

    model_config = {"extra": "ignore"}

    verdicts: list[_VerificationVerdict] = Field(
        default_factory=list,
        description="One verdict per input finding, matched by index",
    )


# ---------------------------------------------------------------------------
# Instruction
# ---------------------------------------------------------------------------

_VERIFICATION_INSTRUCTION = """\
You are an expert code reviewer performing a second-opinion check on flagged findings.

For each finding you receive you will see:
- index: integer (used to match your verdict back to the finding)
- file: the source file path
- line: the line number in the new-file view
- severity: the finding severity ("high", "medium", "low", or "nit")
- message: the reviewer's concern
- evidence: brief code quote supporting the concern (may be empty)
- code_snippet: the relevant lines from the diff, with n: line-number annotations
- file_context: (when available) a wider window of the whole file at the reviewed commit,
  with line numbers, including code the diff does not show
A "Project manifests" section may precede the findings: root files such as pyproject.toml
or package.json, showing the language version and dependencies the project targets.

Your task: decide whether each finding is a REAL problem, using the snippet, the file
context and the manifests.

Output a JSON object {"verdicts": [...]} with one verdict per finding:
  - index: same integer as the input
  - verdict: "confirm" if the concern is real, "reject" if the code shown is fine
  - reason: one concise sentence

Rules:
- "confirm" when the snippet supports or is consistent with the concern — not only when
  the snippet contains direct proof, but also when the concern is plausible given what is visible.
- "reject" when the snippet, the file context or the manifests contradict the concern: for
  example the variable it calls undefined is defined in the file context, the case it says
  is unhandled is handled a few lines away, or the "syntax error" is valid in the language
  version the manifest declares.
- "reject" when the concern depends on context (another file, a caller) that is entirely
  absent and nothing visible makes it plausible.
- Do NOT invent concerns beyond what the message describes.
- Keep reason to one sentence.

Calibration examples:

Example — REJECT:
  message: "loop variable `i` shadows outer variable"
  code_snippet: (shows only one function, no outer scope visible)
  → verdict: "reject"
  reason: No outer scope is present in the snippet; the shadowing concern is not observable.

Example — CONFIRM:
  message: "result list is iterated after .sort() which returns None"
  code_snippet: "42: x = items.sort()\n43:  for item in x:"
  → verdict: "confirm"
  reason: code_snippet directly shows x being assigned None and then iterated.

Example — CONFIRM (uncertainty tie-break):
  message: "database connection may not be closed on exception path"
  code_snippet: (shows `conn = db.connect()` but the exception path is not visible)
  → verdict: "confirm"
  reason: Concern is plausible and the fix is low-risk; keep it for the author to review.

Example — CONFIRM (security vulnerability with partial evidence):
  message: "user input interpolated directly into SQL query — SQL injection risk"
  code_snippet: "87: query = 'SELECT * FROM users WHERE id = ' + user_id"
  → verdict: "confirm"
  reason: String concatenation into a SQL query is directly visible in the snippet;
  this is a well-known injection pattern regardless of how user_id originates.

Severity weighting — adjust your confirmation bar by severity:
- high / medium: lean toward "confirm" unless the snippet clearly shows the concern is
  invalid. These findings represent real risk; prefer keeping them.
- low: "confirm" when the snippet directly supports or is consistent with the claim.
- nit: "reject" unless the snippet makes the issue unambiguous and easy to verify.

Tie-breaker — when genuinely uncertain after applying the above rules:
Prefer "confirm". A false positive that reaches the pipeline is safer than a real bug
that is silently dropped. Apply this only as a last resort, not as the default.
"""


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def create_verification_agent():
    """Create the lightweight verification ADK agent."""
    from google.adk.agents import Agent
    from google.genai import types

    from code_review.config import get_llm_config, get_verification_llm_config
    from code_review.models import (
        get_configured_verification_model,
        get_effective_temperature_for_model,
    )

    llm_cfg = get_llm_config()
    verification_cfg = get_verification_llm_config()
    provider = verification_cfg.provider or llm_cfg.provider
    model = verification_cfg.model or llm_cfg.model
    # Deterministic: this is a binary confirm/reject task.
    _temperature = get_effective_temperature_for_model(provider, model, 0.1)
    generate_content_config = types.GenerateContentConfig(
        **({"temperature": _temperature} if _temperature is not None else {}),
        max_output_tokens=min(llm_cfg.max_output_tokens, _MAX_OUTPUT_TOKENS),
    )
    return Agent(
        model=get_configured_verification_model(),
        name="verification_agent",
        instruction=_VERIFICATION_INSTRUCTION,
        output_schema=_VerificationResult,
        generate_content_config=generate_content_config,
        after_model_callback=lambda callback_context, llm_response: log_adk_llm_usage(
            logger,
            task="verification",
            response=llm_response,
            provider=provider,
            model=model,
        ),
    )


# ---------------------------------------------------------------------------
# Code snippet extraction helper
# ---------------------------------------------------------------------------


def _parse_diff_header_path(header_line: str) -> str:
    """Extract the b-side path from a ``diff --git`` header line.

    Handles both plain (``diff --git a/foo b/foo``) and Git-quoted forms
    (``diff --git "a/path with spaces" "b/path with spaces"``).
    Returns an empty string if the header cannot be parsed.
    """
    # Quoted form: diff --git "a/..." "b/..."
    if '" "b/' in header_line:
        _, _, rest = header_line.partition('" "b/')
        return rest.rstrip('"').strip()
    # Unquoted form: diff --git a/... b/...
    parts = header_line.split(" b/", 1)
    if len(parts) == 2:
        return parts[1].strip()
    return ""


def _extract_snippet_from_annotated(  # NOSONAR — linear scan; splitting adds no clarity
    finding: FindingV1,
    annotated_diff: str,
    diff_paths: frozenset[str],
    radius: int = 4,
) -> str:
    """Return annotated diff lines near ``finding.line`` in ``finding.path``.

    Takes a *pre-annotated* diff (output of ``annotate_diff_with_line_numbers``)
    and the set of normalised paths in that diff so callers can annotate once
    per batch rather than once per finding.

    Returns up to ``2 * radius + 1`` lines centred on the finding's line, or an
    empty string when the file is not found in the diff.
    """
    if not annotated_diff or not finding.path:
        return ""

    from code_review.diff.utils import normalize_path

    norm_target = normalize_path(finding.path)
    if norm_target not in diff_paths:
        return ""

    target_line = finding.line
    window_lines: list[str] = []
    in_file = False

    for raw_line in annotated_diff.splitlines():
        # Detect file boundary via diff header
        if raw_line.startswith("diff --git"):
            current_path = normalize_path(_parse_diff_header_path(raw_line))
            in_file = current_path == norm_target
            if not in_file and window_lines:
                break  # we've left our file after collecting some lines — done
            continue

        if not in_file:
            continue

        # Check if this annotated line is within the radius window
        stripped = raw_line.lstrip()
        line_no: int | None = None
        if ":" in stripped:
            prefix = stripped.split(":", 1)[0]
            if prefix.isdigit():
                line_no = int(prefix)

        if line_no is not None and abs(line_no - target_line) <= radius:
            window_lines.append(raw_line)
        elif line_no is not None and window_lines and line_no > target_line + radius:
            break  # past the window within this file's lines — done

    return "\n".join(window_lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def needs_verification(finding: FindingV1) -> bool:
    """True unless the reviewer explicitly labelled the finding high-confidence."""
    return finding.confidence != "high"


def _cached_fetcher(fetch: FileFetcher | None) -> FileFetcher | None:
    """Wrap ``fetch`` so each path is fetched at most once and failures become None."""
    if fetch is None:
        return None
    cache: dict[str, str | None] = {}

    def fetch_once(path: str) -> str | None:
        if path not in cache:
            try:
                content = fetch(path)
            except Exception as exc:  # noqa: BLE001 - context is best effort
                logger.debug("Verification: could not fetch %s for context: %s", path, exc)
                content = None
            cache[path] = content if isinstance(content, str) and content else None
        return cache[path]

    return fetch_once


def verify_findings(
    findings: list[FindingV1],
    diff_text: str,
    file_fetcher: FileFetcher | None = None,
) -> list[FindingV1]:
    """Run a second-opinion verification pass over findings not marked high-confidence.

    Findings labelled ``confidence="high"`` are returned unchanged. All others,
    including those with no confidence label, are sent to the verification agent
    in batches; only confirmed (or fail-open) findings are kept.

    ``file_fetcher`` returns a file's content at the reviewed head commit. When
    given, each finding is shown with a wider window of its file and the prompt
    includes the project's root manifests.

    Returns a new list preserving the original order.
    """
    if not findings:
        return findings

    high: list[tuple[int, FindingV1]] = []
    to_verify: list[tuple[int, FindingV1]] = []

    for idx, f in enumerate(findings):
        if needs_verification(f):
            to_verify.append((idx, f))
        else:
            high.append((idx, f))

    if not to_verify:
        logger.info(
            "Verification: all %d finding(s) have high confidence; skipping",
            len(findings),
        )
        return findings

    logger.info(
        "Verification: %d high-confidence finding(s) pass through; "
        "%d finding(s) will be verified",
        len(high),
        len(to_verify),
    )

    fetch = _cached_fetcher(file_fetcher)
    manifests = _project_manifests(fetch)

    confirmed: list[tuple[int, FindingV1]] = []
    fail_open_keeps: list[tuple[int, FindingV1]] = []
    total_confirmed = 0
    total_fail_open = 0
    total_rejected = 0

    # Process in batches to keep prompts manageable
    for batch_start in range(0, len(to_verify), _MAX_FINDINGS_PER_BATCH):
        batch = to_verify[batch_start : batch_start + _MAX_FINDINGS_PER_BATCH]
        batch_confirmed, batch_fail_open, batch_rejected = _verify_batch(
            batch, diff_text, fetch=fetch, manifests=manifests
        )
        confirmed.extend(batch_confirmed)
        fail_open_keeps.extend(batch_fail_open)
        total_confirmed += len(batch_confirmed)
        total_fail_open += len(batch_fail_open)
        total_rejected += batch_rejected

    logger.info(
        "Verification: confirmed=%d fail_open=%d rejected=%d (of %d verified)",
        total_confirmed,
        total_fail_open,
        total_rejected,
        len(to_verify),
    )

    # Merge high-confidence + confirmed + fail-open keeps, restoring original order
    merged: list[tuple[int, FindingV1]] = high + confirmed + fail_open_keeps
    merged.sort(key=lambda t: t[0])
    return [f for _, f in merged]


def _verify_batch(
    batch: list[tuple[int, FindingV1]],
    diff_text: str,
    *,
    fetch: FileFetcher | None = None,
    manifests: str = "",
) -> tuple[list[tuple[int, FindingV1]], list[tuple[int, FindingV1]], int]:
    """Verify one batch of findings.

    Returns ``(confirmed, fail_open_keeps, rejected_count)`` where:
    - ``confirmed``: findings explicitly confirmed by the agent.
    - ``fail_open_keeps``: findings kept because the agent failed or omitted a verdict.
    - ``rejected_count``: number of findings the agent explicitly rejected.
    """
    prompt = _build_verification_prompt(batch, diff_text, fetch=fetch, manifests=manifests)
    result = _run_verification_agent(prompt)

    if result is None:
        # Agent invocation failed entirely — keep all as fail-open, none confirmed.
        logger.warning(
            "Verification agent failed for batch of %d finding(s); keeping all as fail-open",
            len(batch),
        )
        return [], list(batch), 0

    verdict_by_index: dict[int, _VerificationVerdict] = {v.index: v for v in result.verdicts}

    confirmed: list[tuple[int, FindingV1]] = []
    fail_open: list[tuple[int, FindingV1]] = []
    rejected = 0

    for local_idx, (original_idx, finding) in enumerate(batch):
        verdict = verdict_by_index.get(local_idx)
        if verdict is None:
            # Agent omitted a verdict — keep as fail-open, do not count as confirmed.
            logger.debug(
                "Verification: no verdict for finding %s:%d — keeping (fail-open)",
                finding.path,
                finding.line,
            )
            fail_open.append((original_idx, finding))
        elif verdict.verdict == "confirm":
            logger.debug(
                "Verification: confirmed %s:%d — %s",
                finding.path,
                finding.line,
                verdict.reason,
            )
            confirmed.append((original_idx, finding))
        else:
            logger.info(
                "Verification: rejected %s:%d (%s) — %s",
                finding.path,
                finding.line,
                finding.code,
                verdict.reason,
            )
            rejected += 1

    return confirmed, fail_open, rejected


def _project_manifests(fetch: FileFetcher | None) -> str:
    """Return the root manifest files that exist, each truncated, as one prompt section."""
    if fetch is None:
        return ""
    sections: list[str] = []
    for name in PROJECT_MANIFESTS:
        content = fetch(name)
        if not content:
            continue
        lines = content.splitlines()
        shown = "\n".join(lines[:_MAX_MANIFEST_LINES])
        if len(lines) > _MAX_MANIFEST_LINES:
            shown += f"\n... ({len(lines) - _MAX_MANIFEST_LINES} more lines)"
        sections.append(f"--- {name} ---\n{shown}")
    return "\n".join(sections)


def _fetch_finding_file(path: str, fetch: FileFetcher) -> str | None:
    """Fetch the file a finding names, tolerating diff-style path prefixes.

    Provider prefixes (``dst://``) and leading slashes are always removed. A leading
    ``a/`` or ``b/`` may be a diff-header prefix or a real top-level directory, so
    the path is tried as given first and with that prefix removed only if it fails.
    """
    from code_review.diff.utils import normalize_path

    as_given = normalize_path(path, strip_git_prefixes=False)
    content = fetch(as_given)
    if content:
        return content
    stripped = normalize_path(path)
    if stripped != as_given:
        return fetch(stripped)
    return None


def _file_context(
    finding: FindingV1,
    fetch: FileFetcher | None,
    radius: int = _FILE_CONTEXT_RADIUS,
) -> str:
    """Return numbered lines of the finding's file around its line, or ""."""
    if fetch is None or not finding.path or not finding.line:
        return ""
    content = _fetch_finding_file(finding.path, fetch)
    if not content:
        return ""
    lines = content.splitlines()
    end_line = finding.line
    if finding.end_line and finding.end_line > finding.line:
        end_line = min(finding.end_line, finding.line + _FILE_CONTEXT_MAX_SPAN)
    start = max(1, finding.line - radius)
    stop = min(len(lines), end_line + radius)
    if start > len(lines):
        return ""
    return "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, stop + 1))


def _build_verification_prompt(
    batch: list[tuple[int, FindingV1]],
    diff_text: str,
    *,
    fetch: FileFetcher | None = None,
    manifests: str = "",
) -> str:
    """Build the user message for one verification batch.

    The diff is annotated and parsed once here, then reused for every finding
    in the batch to avoid O(n) repeated work.
    """
    from code_review.diff.parser import annotate_diff_with_line_numbers, parse_unified_diff
    from code_review.diff.utils import normalize_path

    if diff_text:
        annotated = annotate_diff_with_line_numbers(diff_text)
        diff_paths: frozenset[str] = frozenset(
            normalize_path(h.path) for h in parse_unified_diff(diff_text)
        )
    else:
        annotated = ""
        diff_paths = frozenset()

    blocks: list[str] = []
    if manifests:
        blocks.append(f"Project manifests (repository root, reviewed commit):\n{manifests}\n")
    blocks.append(f"Verify the following {len(batch)} finding(s):\n")
    for local_idx, (_, finding) in enumerate(batch):
        snippet = _extract_snippet_from_annotated(finding, annotated, diff_paths)
        file_context = _file_context(finding, fetch)
        block = (
            f"--- Finding {local_idx} ---\n"
            f"index: {local_idx}\n"
            f"file: {finding.path}\n"
            f"line: {finding.line}\n"
            f"severity: {finding.severity or 'unknown'}\n"
            f"message: {finding.message}\n"
            f"evidence: {finding.evidence or '(none provided)'}\n"
            f"code_snippet:\n{snippet or '(not available)'}\n"
        )
        if file_context:
            block += f"file_context:\n{file_context}\n"
        blocks.append(block)
    return "\n".join(blocks)


def _run_verification_agent(prompt: str) -> _VerificationResult | None:
    """Invoke the verification agent and return the parsed result, or None on error."""
    try:
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        from code_review.adk_runner import create_runner
        from code_review.orchestration.runner_utils import _run_agent_and_collect_response

        agent = create_verification_agent()
        session_service = InMemorySessionService()
        runner = create_runner(
            agent=agent,
            app_name="code_review",
            session_service=session_service,
            auto_create_session=True,
        )
        session_id = f"verification/{uuid.uuid4().hex[:12]}"
        content = types.Content(role="user", parts=[types.Part(text=prompt)])

        raw_response = _run_agent_and_collect_response(runner, session_id, content)

        if not raw_response.strip():
            logger.warning("Verification agent returned empty response")
            return None

        return _parse_verification_result(raw_response)

    except Exception as exc:
        logger.warning("Verification agent invocation failed: %s", exc, exc_info=True)
        return None


def _parse_verification_result(text: str) -> _VerificationResult | None:
    """Parse the agent's JSON text into a _VerificationResult, or None on failure."""
    for candidate in iter_json_candidates(text):
        try:
            data = json.loads(candidate)
            return _VerificationResult.model_validate(data)
        except Exception:
            continue

    logger.warning("Could not parse verification result from: %.200s", text)
    return None
