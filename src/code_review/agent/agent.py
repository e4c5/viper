"""ADK agent definition for code review."""

from __future__ import annotations

from typing import TYPE_CHECKING

from code_review.agent.tools.gitea_tools import create_gitea_tools
from code_review.models import get_configured_model
from code_review.providers.base import ProviderInterface

if TYPE_CHECKING:
    from google.adk.agents import Agent

# Base instruction; review_standards fragment is appended by runner
BASE_INSTRUCTION = """
You are a code review agent. You will receive PR details (owner, repo, pr_number, head_sha).
Use get_pr_diff to fetch the diff, get_file_content for AGENTS.md/README/.cursor/rules context.
Call get_existing_review_comments to get the ignore list (manually resolved issues). Do not post comments for issues that match (path, body_hash) in the ignore list.
Analyze the diff for bugs, style, security, and best practices. Consider the language/framework.
Use post_review_comment for each finding: path, line, and body with [Critical]/[Suggestion]/[Info] prefix. Skip any finding that was manually resolved.
"""


def create_review_agent(
    provider: ProviderInterface,
    review_standards: str = "",
) -> Agent:
    """Create the code review LlmAgent with tools and instruction."""
    from google.adk.agents import Agent

    tools = create_gitea_tools(provider)
    instruction = BASE_INSTRUCTION
    if review_standards:
        instruction = instruction.rstrip() + "\n\n" + review_standards

    return Agent(
        model=get_configured_model(),
        name="code_review_agent",
        instruction=instruction,
        tools=tools,
    )
