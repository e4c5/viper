"""Extract GitHub Issue, Jira ticket, and Confluence page references from text.

References are extracted from PR titles, descriptions, and commit messages so that
the code review agent can fetch and include the relevant context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class ReferenceType(str, Enum):
    GITHUB_ISSUE = "github_issue"
    JIRA = "jira"
    CONFLUENCE = "confluence"


@dataclass
class Reference:
    ref_type: ReferenceType
    # Identifier meaningful to the fetcher:
    #   github_issue -> "<owner>/<repo>/<issue_number>" or just "<number>" (same-repo shorthand)
    #   jira         -> "<PROJECT-NNN>" ticket key
    #   confluence   -> "<page_id>" or full URL
    identifier: str
    url: str | None = None
    # Deduplication key — used to avoid fetching the same resource twice.
    key: str = field(init=False)

    def __post_init__(self) -> None:
        self.key = f"{self.ref_type.value}:{self.identifier}"


# ---------------------------------------------------------------------------
# Compiled regex patterns
# ---------------------------------------------------------------------------

# GitHub issue shorthand: #42, GH-42, gh-42
_GH_SHORT_RE = re.compile(r"(?:GH-|#)(\d+)", re.IGNORECASE)

# GitHub issue URL: github.com/{owner}/{repo}/issues/{number}
_GH_URL_RE = re.compile(
    r"https?://[^/\s]*github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)",
    re.IGNORECASE,
)

# Jira ticket key: ABC-123 (uppercase word chars, dash, digits)
# We require the project key to be ALL_CAPS or mixed to avoid false positives.
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")

# Jira browse URL: .../browse/PROJECT-123
_JIRA_URL_RE = re.compile(
    r"https?://[^/\s]+/browse/([A-Z][A-Z0-9_]+-\d+)",
    re.IGNORECASE,
)

# Confluence page URL: .../wiki/spaces/.../pages/{page_id}[/...]
_CONFLUENCE_URL_RE = re.compile(
    r"(https?://[^/\s]+/wiki/(?:spaces/[^/\s]+/pages/(\d+)|display/[^/\s]+/[^\s]+))",
    re.IGNORECASE,
)

# Confluence page URL variant: .../pages/{page_id}
_CONFLUENCE_PAGE_ID_RE = re.compile(
    r"https?://[^/\s]+/(?:wiki/)?pages/(\d+)",
    re.IGNORECASE,
)


def extract_references(
    text: str,
    *,
    # Caller context for resolving same-repo shorthand issues
    owner: str = "",
    repo: str = "",
    # Optional allowlist of Jira project key prefixes (e.g. ["PROJ", "MYAPP"]).
    # When non-empty, only keys whose prefix matches are returned.
    jira_project_keys: list[str] | None = None,
) -> list[Reference]:
    """Return deduplicated list of References found in *text*.

    Extracts:
    - GitHub issue references (#42, GH-42, github.com URLs)
    - Jira ticket keys (ABC-123, Jira browse URLs)
    - Confluence page URLs
    """
    seen: set[str] = set()
    refs: list[Reference] = []

    def _add(ref: Reference) -> None:
        if ref.key not in seen:
            seen.add(ref.key)
            refs.append(ref)

    if not text:
        return refs

    # --- GitHub issue URLs (authoritative — include owner/repo) ---
    for m in _GH_URL_RE.finditer(text):
        gh_owner, gh_repo, number = m.group(1), m.group(2), m.group(3)
        identifier = f"{gh_owner}/{gh_repo}/{number}"
        _add(Reference(
            ref_type=ReferenceType.GITHUB_ISSUE,
            identifier=identifier,
            url=m.group(0),
        ))

    # --- GitHub shorthand #42 / GH-42 (only if owner+repo context is provided) ---
    if owner and repo:
        for m in _GH_SHORT_RE.finditer(text):
            number = m.group(1)
            identifier = f"{owner}/{repo}/{number}"
            _add(Reference(
                ref_type=ReferenceType.GITHUB_ISSUE,
                identifier=identifier,
                url=f"https://github.com/{owner}/{repo}/issues/{number}",
            ))

    # --- Jira browse URLs (authoritative) ---
    for m in _JIRA_URL_RE.finditer(text):
        key = m.group(1).upper()
        if _jira_key_allowed(key, jira_project_keys):
            _add(Reference(
                ref_type=ReferenceType.JIRA,
                identifier=key,
                url=m.group(0),
            ))

    # --- Jira bare keys (e.g. PROJ-123) ---
    for m in _JIRA_KEY_RE.finditer(text):
        key = m.group(1).upper()
        if _jira_key_allowed(key, jira_project_keys):
            _add(Reference(
                ref_type=ReferenceType.JIRA,
                identifier=key,
            ))

    # --- Confluence page URLs ---
    for m in _CONFLUENCE_URL_RE.finditer(text):
        full_url = m.group(1)
        page_id = m.group(2) or ""  # numeric page ID when present
        identifier = page_id if page_id else full_url
        _add(Reference(
            ref_type=ReferenceType.CONFLUENCE,
            identifier=identifier,
            url=full_url,
        ))

    return refs


def _jira_key_allowed(key: str, project_keys: list[str] | None) -> bool:
    """Return True if key passes the optional project-key prefix filter."""
    if not project_keys:
        return True
    prefix = key.split("-")[0]
    return prefix in project_keys
