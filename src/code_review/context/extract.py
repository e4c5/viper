"""Conservative extraction of issue/ticket/page references from PR text."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from code_review.context.types import ContextReference, ReferenceType

# Jira-style keys: at least two letters in project part, then -digits
_JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,10}-\d+)\b")

# GitHub issue URLs (github.com or enterprise hosts)
_GH_ISSUE_URL = re.compile(
    r"https?://([^/\s]+)/([^/\s]+)/([^/\s]+)/issues/(\d+)\b",
    re.IGNORECASE,
)
_GL_ISSUE_URL = re.compile(
    r"https?://([^/\s]+)/([^\s?#]+?)/(?:-/)?issues/(\d+)\b",
    re.IGNORECASE,
)

# Confluence Cloud/Data Center: .../pages/<id>/... or .../spaces/.../pages/<id>/...
_CONFLUENCE_PAGE_URL = re.compile(
    r"https?://[^/\s]+/(?:wiki/)?(?:spaces/[^/\s]+/)?pages/(\d+)(?:/|\?|#|\b)",
    re.IGNORECASE,
)
# Confluence Server/DC older action URL: .../pages/viewpage.action?pageId=<id>
_CONFLUENCE_ACTION_URL = re.compile(
    r"https?://[^/\s]+/pages/viewpage\.action[^/\s]*[?&]pageId=(\d+)\b",
    re.IGNORECASE,
)

# Optional Jira browse URL
_JIRA_BROWSE_URL = re.compile(
    r"https?://[^/\s]+/browse/([A-Z][A-Z0-9]{1,10}-\d+)\b",
    re.IGNORECASE,
)

_GH_PREFIX = re.compile(r"\bGH-(\d+)\b", re.IGNORECASE)
# Same-repo #NNN only when explicitly allowed (GitHub same repo)
_HASH_ISSUE = re.compile(r"(?<![\w#])#(\d{1,7})\b")


def _strip_markdown_code_fences(text: str) -> str:
    """Remove fenced code blocks to avoid matching refs inside code/stack traces."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("```", i):
            end = text.find("```", i + 3)
            if end == -1:
                break
            i = end + 3
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _append_github_refs(
    add_ref,
    scanned: str,
    owner: str,
    repo: str,
    *,
    allow_hash_issue: bool,
) -> None:
    for m in _GH_ISSUE_URL.finditer(scanned):
        host, org, rname, num = m.group(1), m.group(2), m.group(3), m.group(4)
        if "github" not in host.lower():
            continue
        ref = f"{org}/{rname}#{num}"
        add_ref(ReferenceType.GITHUB_ISSUE, ref, ref)
    for m in _GH_PREFIX.finditer(scanned):
        num = m.group(1)
        ref = f"{owner}/{repo}#{num}"
        add_ref(ReferenceType.GITHUB_ISSUE, ref, ref)
    if not allow_hash_issue:
        return
    for m in _HASH_ISSUE.finditer(scanned):
        num = m.group(1)
        ref = f"{owner}/{repo}#{num}"
        add_ref(ReferenceType.GITHUB_ISSUE, ref, ref)


def _append_jira_refs(add_ref, scanned: str) -> None:
    for m in _JIRA_BROWSE_URL.finditer(scanned):
        key = m.group(1).upper()
        add_ref(ReferenceType.JIRA, key, key)
    for m in _JIRA_KEY.finditer(scanned):
        key = m.group(1).upper()
        add_ref(ReferenceType.JIRA, key, key)


def _append_gitlab_refs(add_ref, scanned: str, *, scm_provider: str) -> None:
    for m in _GL_ISSUE_URL.finditer(scanned):
        host, project_path, issue_num = m.group(1), m.group(2), m.group(3)
        # Conservative: allow obvious GitLab hosts; also allow any host when reviewing on GitLab.
        if scm_provider != "gitlab" and "gitlab" not in host.lower():
            continue
        project = project_path.strip("/")
        if not project:
            continue
        ref = f"{project}#{issue_num}"
        add_ref(ReferenceType.GITLAB_ISSUE, ref, ref)


def _append_confluence_refs(add_ref, scanned: str) -> None:
    for m in _CONFLUENCE_PAGE_URL.finditer(scanned):
        pid = m.group(1)
        add_ref(ReferenceType.CONFLUENCE, pid, f"confluence-page:{pid}")
    for m in _CONFLUENCE_ACTION_URL.finditer(scanned):
        pid = m.group(1)
        add_ref(ReferenceType.CONFLUENCE, pid, f"confluence-page:{pid}")


def extract_context_references(
    scm_provider: Literal["gitea", "github", "gitlab", "bitbucket", "bitbucket_server"],
    owner: str,
    repo: str,
    text_segments: Sequence[str],
    *,
    github_issue_same_repo: bool = True,
    extract_jira: bool = True,
    extract_gitlab: bool = True,
    extract_confluence: bool = True,
    extract_github: bool = True,
) -> list[ContextReference]:
    """
    Parse PR title, description, and commit messages (``text_segments`` in order).

    ``#NNN`` is recognised only when ``scm_provider == \"github\"`` and
    ``github_issue_same_repo`` is True (assumed same repository).
    """
    raw = "\n".join(s for s in text_segments if s)
    scanned = _strip_markdown_code_fences(raw)

    seen: set[tuple[ReferenceType, str]] = set()
    out: list[ContextReference] = []

    def add(ref_type: ReferenceType, external_id: str, display: str) -> None:
        key = (ref_type, external_id)
        if key in seen:
            return
        seen.add(key)
        out.append(ContextReference(ref_type=ref_type, external_id=external_id, display=display))

    if extract_github:
        _append_github_refs(
            add,
            scanned,
            owner,
            repo,
            allow_hash_issue=(
                scm_provider == "github" and github_issue_same_repo and bool(owner) and bool(repo)
            ),
        )
    if extract_jira:
        _append_jira_refs(add, scanned)
    if extract_gitlab:
        _append_gitlab_refs(add, scanned, scm_provider=scm_provider)
    if extract_confluence:
        _append_confluence_refs(add, scanned)

    return out
