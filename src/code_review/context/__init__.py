"""Context enrichment for code review: GitHub Issues, Jira, Confluence."""

from code_review.context.extractor import Reference, ReferenceType, extract_references
from code_review.context.fetcher import ContextFetcher

__all__ = [
    "ContextFetcher",
    "Reference",
    "ReferenceType",
    "extract_references",
]
