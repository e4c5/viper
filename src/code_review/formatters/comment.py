"""Format findings as inline comment bodies with severity prefix and location consistency."""

from code_review.schemas.findings import FindingV1

# Canonical severity labels for comment body prefix (Phase 4.1)
SEVERITY_LABELS: dict[str, str] = {
    "critical": "[Critical]",
    "suggestion": "[Suggestion]",
    "info": "[Info]",
}


def finding_to_comment_body(f: FindingV1) -> str:
    """
    Format a finding as inline comment body with [Critical]/[Suggestion]/[Info] prefix.
    Location (path, line, optional end_line) is carried by the runner when posting;
    this returns only the body text.
    """
    label = SEVERITY_LABELS.get(f.severity.lower(), f"[{f.severity.title()}]")
    return f"{label} {f.get_body()}"
