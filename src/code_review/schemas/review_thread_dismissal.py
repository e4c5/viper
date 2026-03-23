"""Structured review-thread context for reply-dismissal classification (Phase E)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReviewThreadDismissalEntry(BaseModel):
    """One comment in a PR review thread, in chronological order."""

    model_config = {"extra": "ignore"}

    comment_id: str = Field(default="", description="Provider-specific comment / note id")
    author_login: str = Field(default="", description="Username or login when available")
    body: str = ""
    created_at: str = Field(default="", description="ISO timestamp when exposed by API")


class ReviewThreadDismissalContext(BaseModel):
    """Thread snapshot for the reply-dismissal agent and gate exclusion."""

    gate_exclusion_stable_id: str = Field(
        ...,
        description="Matches UnresolvedReviewItem.stable_id when agreed verdict excludes thread",
    )
    entries: list[ReviewThreadDismissalEntry] = Field(default_factory=list)
