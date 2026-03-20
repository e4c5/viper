from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReferenceType(str, Enum):
    GITHUB_ISSUE = "github_issue"
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
