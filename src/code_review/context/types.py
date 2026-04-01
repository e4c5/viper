from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReferenceType(str, Enum):
    GITHUB_ISSUE = "github_issue"
    GITLAB_ISSUE = "gitlab_issue"
    JIRA = "jira"
    CONFLUENCE = "confluence"


@dataclass(frozen=True)
class ContextReference:
    """Canonical external reference extracted from PR text."""

    ref_type: ReferenceType
    external_id: str
    """Stable id for cache key, e.g. issue number as str, Jira key, Confluence page id."""

    display: str
    """Human-readable label for logs and distillation."""


@dataclass(frozen=True)
class ExternalCredentials:
    """Resolved external API endpoints and credentials used for context fetching."""

    github_api: str = ""
    github_token: str = ""
    gitlab_api: str = ""
    gitlab_token: str = ""
    jira_email: str = ""
    jira_token: str = ""
    confluence_email: str = ""
    confluence_token: str = ""
