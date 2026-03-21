"""Format findings as inline comment bodies with severity prefix and location consistency."""

import re
from typing import Literal

from code_review.schemas.findings import FindingV1

# Canonical severity labels for comment body prefix (Phase 4.1)
SEVERITY_LABELS: dict[str, str] = {
    "high": "[High]",
    "medium": "[Medium]",
    "low": "[Low]",
    "nit": "[Nit]",
}

# Emoji markers by severity for quick visual scanning in SCM UIs.
SEVERITY_EMOJIS: dict[str, str] = {
    "high": "🛑",
    "medium": "⚠️",
    "low": "💡",
    "nit": "ℹ️",
}

# Strip any leading "[Something]" tags the agent may have already added to the body
_LEADING_TAGS_RE = re.compile(r"^(?:\s*\[[^\]]+\])+\s*", flags=re.IGNORECASE)

# Matches a single outer fenced code block (optional language tag) so it can be
# unwrapped before insertion into a ```suggestion block.
_CODE_FENCE_RE = re.compile(r"^```[^\n]*\n([\s\S]*?)\n?```\s*$", re.MULTILINE)


def _strip_code_fence(text: str) -> str:
    """Remove a single outer fenced code block wrapper if present, return raw content."""
    m = _CODE_FENCE_RE.match(text.strip())
    return m.group(1) if m else text


def render_suggestion_block(body: str, patch: str | None) -> str:
    """
    Append a ```suggestion block to the body when a patch is provided.
    Used by providers that support suggestions (GitHub, GitLab, Gitea, Bitbucket).
    Any outer fenced code block already wrapping the patch is stripped first so
    nested fences don't prematurely close the suggestion block.
    """
    if not patch:
        return body
    return f"{body}\n\n```suggestion\n{_strip_code_fence(patch)}\n```"


def _strip_leading_tags(text: str) -> str:
    """Remove duplicated [Tag] prefixes from the body before we add our own."""
    return _LEADING_TAGS_RE.sub("", text).lstrip()


def _strip_path_prefixes(text: str) -> str:
    """Remove dst:// and src:// from path-like text so displayed comments don't show them."""
    if not text:
        return text
    out = text.replace("dst://", "").replace("src://", "")
    return out


def finding_to_comment_body(
    f: FindingV1,
    use_collapsible_prompt: bool = True,
) -> str:
    """
    Format a finding as inline comment body with a [High]/[Medium]/[Low]/[Nit] prefix.
    Location (path, line, optional end_line) is carried by the runner when posting;
    this returns only the body text.
    When use_collapsible_prompt is False (e.g. Bitbucket), the agent prompt is
    formatted as plain text instead of <details>/<summary> to avoid raw tags in the UI.
    """
    severity_key = f.severity.lower()
    label = SEVERITY_LABELS.get(severity_key, f"[{f.severity.title()}]")

    body = _strip_leading_tags(f.get_body())
    body = _strip_path_prefixes(body)

    if not body:
        main = label
    else:
        main = f"{label} {body}"

    # Optionally append block containing an agent fix prompt, when provided.
    if f.agent_fix_prompt:
        prompt_text = _strip_path_prefixes(f.agent_fix_prompt)
        if use_collapsible_prompt:
            prompt_block = (
                "\n\n"
                "<details>\n"
                "<summary>Prompt for AI Agents</summary>\n\n"
                f"{prompt_text}\n"
                "</details>"
            )
        else:
            prompt_block = (
                "\n\n---\n**Prompt for AI Agents**\n\n"
                f"{prompt_text}"
            )
        return main + prompt_block

    return main


def infer_severity_from_comment_body(body: str) -> Literal["high", "medium", "low", "nit", "unknown"]:
    """Infer Viper-style severity from a review comment body ([High]/[Medium]/…).

    Strips HTML comment blocks (e.g. fingerprint markers) first, then looks for
    canonical ``SEVERITY_LABELS`` near the start of the visible text.
    """
    if not body or not body.strip():
        return "unknown"
    cleaned = body.strip()
    while True:
        start = cleaned.find("<!--")
        if start == -1:
            break
        end = cleaned.find("-->", start)
        if end == -1:
            cleaned = cleaned[:start].strip()
            break
        cleaned = (cleaned[:start] + cleaned[end + 3 :]).strip()
    head = cleaned[:500]
    for sev in ("high", "medium", "low", "nit"):
        label = SEVERITY_LABELS[sev]
        pos = head.find(label)
        if pos != -1 and pos < 160:
            return sev
    return "unknown"


_SEV_RANK: dict[str, int] = {"high": 4, "medium": 3, "low": 2, "nit": 1, "unknown": 0}


def max_inferred_severity(
    a: Literal["high", "medium", "low", "nit", "unknown"],
    b: Literal["high", "medium", "low", "nit", "unknown"],
) -> Literal["high", "medium", "low", "nit", "unknown"]:
    """Return the stronger of two inferred severities (for multi-note / multi-comment threads)."""
    return a if _SEV_RANK.get(a, 0) >= _SEV_RANK.get(b, 0) else b
