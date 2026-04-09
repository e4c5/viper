"""ADK agent for generating high-level PR review summaries."""

from __future__ import annotations

import logging
from typing import Any

from code_review.config import get_llm_config
from code_review.models import get_configured_model
from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)

SUMMARY_INSTRUCTION = """\
You are a Distinguished Software Engineer and expert reviewer.
Your task is to provide a high-level, strictly technical summary of a Pull Request review.

INPUTS:
- PR Metadata: Title, Description, and list of changed files.
- Findings: A list of specific code quality issues identified during the review, grouped by severity.

GOAL:
Produce a concise, professional, and actionable Markdown summary that helps the author understand the overall impact of the review.

TONE:
- Strictly Technical.
- Professional and objective.
- No conversational filler or generic praise (avoid "Great job", "I have reviewed", etc.).
- Be direct and high-signal.

STRUCTURE:
1. **Summary**: A 1-2 sentence high-level technical assessment of the changes. Include a one-line
   metrics count on its own line, e.g.: `3 high · 5 medium · 2 nit findings.`
2. **Walkthrough**: Briefly group the changes into logical functional areas (e.g., "API Endpoints",
   "Data Layer", "Security Configuration").
3. **Findings Overview**:
   - Categorize the findings by severity (High, Medium, Nit).
   - Summarize the main themes of the findings (e.g., "Concurrency issues in the task runner",
     "Missing input validation in auth middleware").
4. **Narrative Summary**: A short, flowing paragraph (3-5 sentences) that tells the story of this
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
- When the Findings input is "No specific findings identified." (empty findings list), produce a
  short, positive summary: note that the review found no issues, briefly describe what changed
  (from the PR metadata), and keep the output to 3-5 sentences. Do NOT invent findings or pad
  the output with generic advice.
"""


def create_summary_agent():
    """Create the PR Summary agent."""
    from google.adk.agents import Agent
    from google.genai import types

    llm_cfg = get_llm_config()
    generate_content_config = types.GenerateContentConfig(
        temperature=0.2, # Lower temperature for objective summaries
        max_output_tokens=llm_cfg.max_output_tokens,
    )

    return Agent(
        model=get_configured_model(),
        name="summary_agent",
        instruction=SUMMARY_INSTRUCTION,
        generate_content_config=generate_content_config,
    )

def generate_pr_summary(
    agent,
    pr_info: Any,
    findings: list[FindingV1],
    changed_paths: list[str],
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

    prompt = f"""\
PR Title: {getattr(pr_info, 'title', 'Unknown')}
PR Description: {getattr(pr_info, 'description', 'No description provided')}
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
