"""Context-aware review: extract references, fetch external context, distill, optional RAG."""

from code_review.context.errors import ContextAwareFatalError
from code_review.context.extract import extract_context_references
from code_review.context.pipeline import build_context_brief_for_pr
from code_review.context.types import ContextReference, ReferenceType
from code_review.context.validation import validate_context_aware_sources

__all__ = [
    "ContextAwareFatalError",
    "ContextReference",
    "ReferenceType",
    "build_context_brief_for_pr",
    "extract_context_references",
    "validate_context_aware_sources",
]
