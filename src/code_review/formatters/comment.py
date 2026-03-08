"""Format findings as inline comment bodies with severity prefix and location consistency."""

import re

from code_review.schemas.findings import FindingV1

# Canonical severity labels for comment body prefix (Phase 4.1)
SEVERITY_LABELS: dict[str, str] = {
    "critical": "[Critical]",
    "suggestion": "[Suggestion]",
    "info": "[Info]",
}

# Emoji markers by severity for quick visual scanning in SCM UIs.
SEVERITY_EMOJIS: dict[str, str] = {
    "critical": "🛑",
    "suggestion": "💡",
    "info": "ℹ️",
}

# Strip any leading "[Something]" tags the agent may have already added to the body
_LEADING_TAGS_RE = re.compile(r"^(?:\s*\[[^\]]+\])+\s*", flags=re.IGNORECASE)


def _strip_leading_tags(text: str) -> str:
    """Remove duplicated [Tag] prefixes from the body before we add our own."""
    return _LEADING_TAGS_RE.sub("", text).lstrip()


def finding_to_comment_body(
    f: FindingV1,
    use_collapsible_prompt: bool = True,
) -> str:
    """
    Format a finding as inline comment body with a [Critical]/[Suggestion]/[Info] prefix.
    Location (path, line, optional end_line) is carried by the runner when posting;
    this returns only the body text.
    When use_collapsible_prompt is False (e.g. Bitbucket), the agent prompt is
    formatted as plain text instead of <details>/<summary> to avoid raw tags in the UI.
    """
    severity_key = f.severity.lower()
    label = SEVERITY_LABELS.get(severity_key, f"[{f.severity.title()}]")

    body = _strip_leading_tags(f.get_body())

    if not body:
        main = label
    else:
        main = f"{label} {body}"

    # Optionally append block containing an agent fix prompt, when provided.
    if f.agent_fix_prompt:
        if use_collapsible_prompt:
            prompt_block = (
                "\n\n"
                "<details>\n"
                "<summary>Prompt for AI Agents</summary>\n\n"
                f"{f.agent_fix_prompt}\n"
                "</details>"
            )
        else:
            prompt_block = (
                "\n\n---\n**Prompt for AI Agents**\n\n"
                f"{f.agent_fix_prompt}"
            )
        return main + prompt_block

    return main
