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
- Findings: A list of specific code quality issues identified during the review.

GOAL:
Produce a concise, professional, and actionable Markdown summary that helps the author understand the overall impact of the review.

TONE:
- Strictly Technical.
- Professional and objective.
- No conversational filler or generic praise (avoid "Great job", "I have reviewed", etc.).
- Be direct and high-signal.

STRUCTURE:
1. **Summary**: A 1-2 sentence high-level technical assessment of the changes.
2. **Walkthrough**: Briefly group the changes into logical functional areas (e.g., "API Endpoints", "Data Layer", "Security Configuration").
3. **Findings Overview**:
   - Categorize the findings by severity (High, Medium, Nit).
   - Summarize the main themes of the findings (e.g., "Concurrency issues in the task runner", "Missing input validation in auth middleware").
4. **Conclusion**: A brief statement on the overall review effort or readiness (e.g., "Estimated review effort: Medium. Requires structural changes to error handling before merge.").

FORMATTING:
- Use standard Markdown headings and lists.
- Do NOT use HTML tags unless necessary.
- Keep it compact and dense with technical information.
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
    from google.adk.runners import AgentRunner

    # Prepare the prompt payload
    findings_summary = ""
    if findings:
        findings_summary = "\n".join([
            f"- [{f.severity.upper()}] {f.path}:{f.line} - {f.message}"
            for f in findings
        ])
    else:
        findings_summary = "No specific findings identified."

    prompt = f"""\
PR Title: {getattr(pr_info, 'title', 'Unknown')}
PR Description: {getattr(pr_info, 'description', 'No description provided')}
Changed Files: {', '.join(changed_paths)}

Findings:
{findings_summary}
"""

    runner = AgentRunner(agent)
    # Since we didn't provide a schema, we expect raw text response
    response = runner.generate_content(prompt)
    
    # ADK Agent returns the text from the last message in the thread
    # We can also use runner.thread.last_message.text
    return response.text if hasattr(response, 'text') else str(response)
