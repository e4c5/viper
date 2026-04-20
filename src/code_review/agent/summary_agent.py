"""ADK agent for generating high-level PR review summaries."""

from __future__ import annotations

import logging
import re
from typing import Any

from code_review.config import get_llm_config, get_summary_llm_config
from code_review.models import (
    get_configured_summary_model,
    get_effective_temperature_for_model,
)
from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)

SUMMARY_INSTRUCTION = """\
You are a Distinguished Software Engineer and expert reviewer.
Your task is to provide a high-level, strictly technical summary of a Pull Request review.

INPUTS:
- PR Metadata: Title, Description, and list of changed files.
- Findings: A list of specific code quality issues identified during the review,
  grouped by severity.

GOAL:
Produce a concise, professional, and actionable Markdown summary that helps the author
understand the overall impact of the review.

TONE:
- Strictly Technical.
- Professional and objective.
- No conversational filler or generic praise (avoid "Great job", "I have reviewed", etc.).
- Be direct and high-signal.

STRUCTURE:
## Summary
A 1-2 sentence high-level technical assessment of the changes. Include a one-line
metrics count on its own line, e.g.: `3 high · 5 medium · 2 nit findings.`

## Description
A concise, functional description of what this PR does — what changed and why,
written from the author's perspective (2-4 sentences). Focus purely on the code changes:
what was added, removed, or refactored, and the logical intent. Do NOT mention findings here.

## Walkthrough
Briefly group the changes into logical functional areas (e.g., "API Endpoints",
"Data Layer", "Security Configuration").

## Findings Overview
- Categorize the findings by severity (High, Medium, Nit).
- Summarize the main themes of the findings (e.g., "Concurrency issues in the task runner",
  "Missing input validation in auth middleware").

## Narrative Summary
A short, flowing paragraph (3-5 sentences) that tells the story of this
PR — what it accomplishes, what the most significant findings are, and what the author should
prioritize addressing first. This replaces a generic readiness statement and should read as a
cohesive, human-readable conclusion.

FORMATTING:
- Use standard Markdown headings and lists.
- Do NOT use HTML tags unless necessary.
- Keep it compact and dense with technical information.

LENGTH:
- The entire summary MUST be 400 words or fewer. Be ruthlessly concise.
- If findings are numerous, summarize themes rather than listing every finding individually.

NO-FINDINGS CASE:
- When the Findings input is "No specific findings identified." (empty findings list), produce:
  ## Summary
  One sentence noting no issues were identified.
  ## Description
  2-4 sentences describing what changed (from PR metadata/diff context).
  Skip the Walkthrough, Findings Overview, and Narrative Summary entirely.
  Keep the total output very short. Do NOT invent findings or pad with generic advice.

INCREMENTAL UPDATES:
If the input includes "Incremental Review Context", this is an incremental update review.
- Focus your assessment, walkthrough, and narrative summary ONLY on the new changes provided in
  this run (the specified incremental commits and changed files).
- Do NOT re-summarize the entire PR or previous commits that are not part of this update.
- The Narrative Summary should tell the story of THIS specific update (e.g., "This update addresses
  previous feedback by...", "This commit adds missing validation mentioned in the last review").
"""


def create_summary_agent():
    """Create the PR Summary agent."""
    from google.adk.agents import Agent
    from google.genai import types

    llm_cfg = get_llm_config()
    summary_cfg = get_summary_llm_config()
    provider = summary_cfg.provider or llm_cfg.provider
    model = summary_cfg.model or llm_cfg.model
    _temperature = get_effective_temperature_for_model(
        provider, model, 0.2
    )  # lower temperature for objective summaries
    generate_content_config = types.GenerateContentConfig(
        **({"temperature": _temperature} if _temperature is not None else {}),
        max_output_tokens=llm_cfg.max_output_tokens,
    )

    return Agent(
        model=get_configured_summary_model(),
        name="summary_agent",
        instruction=SUMMARY_INSTRUCTION,
        generate_content_config=generate_content_config,
    )

def generate_pr_summary(
    agent,
    pr_info: Any,
    findings: list[FindingV1],
    changed_paths: list[str],
    incremental_base_sha: str = "",
    incremental_commits: list[str] | None = None,
) -> str:
    """Generate a Markdown summary using the summary agent."""
    import uuid

    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from code_review.orchestration.runner_utils import _run_agent_and_collect_response

    # Pre-group findings by severity so the LLM sees priority order naturally.
    severity_order = ["high", "medium", "low", "nit"]
    grouped: dict[str, list[FindingV1]] = {s: [] for s in severity_order}
    for f in findings:
        grouped.get(f.severity, grouped["nit"]).append(f)

    if findings:
        findings_lines: list[str] = []
        for severity in severity_order:
            items = grouped[severity]
            if items:
                findings_lines.append(f"\n{severity.upper()} ({len(items)}):")
                findings_lines.extend(
                    f"  - {f.path}:{f.line} - {f.message}" for f in items
                )
        findings_summary = "\n".join(findings_lines)
    else:
        findings_summary = "No specific findings identified."

    commits_text = ""
    if incremental_commits:
        commits_list = "\n".join(f"  - {msg}" for msg in incremental_commits)
        commits_text = f"\nIncremental commits in this update:\n{commits_list}"

    incremental_context = ""
    if incremental_base_sha or incremental_commits:
        base_ref = (
            f"from {incremental_base_sha[:12]}" if incremental_base_sha else "from unknown base"
        )
        incremental_context = f"\nIncremental Review Context: {base_ref}{commits_text}\n"

    pr_desc = getattr(pr_info, "description", "").strip()
    description_part = f"PR Description: {pr_desc}\n" if pr_desc else ""

    prompt = f"""\
PR Title: {getattr(pr_info, 'title', 'Unknown')}
{description_part}{incremental_context}
Changed Files: {', '.join(changed_paths)}

Findings:
{findings_summary}
"""

    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="code_review",
        session_service=session_service,
        auto_create_session=True,
    )
    session_id = f"summary/{uuid.uuid4().hex[:12]}"
    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    return _run_agent_and_collect_response(runner, session_id, content)


def split_summary_for_pr_description(full_text: str) -> tuple[str, str]:
    """Split LLM summary into (pr_description_part, comment_part).

    The PR description part contains the **Summary** and **Description** sections
    (sections 1 and 2 of the SUMMARY_INSTRUCTION structure) — these describe
    *what the PR does* and are suitable as a GitHub PR description body.

    The comment part starts from **Walkthrough** onward and contains the detailed
    review analysis (findings overview, narrative summary, etc.).

    If no natural split point is found (e.g. the LLM omitted the Walkthrough heading
    in a no-findings response), the full text is returned as the PR description part
    and the comment part is empty.
    """
    match = re.search(
        r'^[ \t]*(?:#{1,6}[ \t]+|\d+\.[ \t]+\*\*|\*\*)[ \t]*Walkthrough\b',
        full_text,
        re.MULTILINE | re.IGNORECASE,
    )
    if not match:
        return full_text.strip(), ""
    return full_text[: match.start()].strip(), full_text[match.start() :].strip()
