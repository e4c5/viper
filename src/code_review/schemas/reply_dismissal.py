"""Pydantic contract for reply-dismissal agent output (Phase E)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ReplyDismissalVerdictV1(BaseModel):
    """LLM output: whether a human reply adequately addresses the review thread."""

    model_config = {"extra": "ignore"}

    version: Literal["1"] = Field(
        default="1",
        description="Contract version; only '1' is supported.",
    )
    verdict: Literal["agreed", "disagreed"] = Field(
        ...,
        description="agreed = thread may be excluded from gate; disagreed = keep blocking",
    )
    reply_text: str = Field(
        default="",
        description="Required when verdict is disagreed: short SCM reply to the author",
    )

    @model_validator(mode="after")
    def disagreed_requires_reply_text(self) -> ReplyDismissalVerdictV1:
        if self.verdict == "disagreed" and not self.reply_text.strip():
            raise ValueError("reply_text is required when verdict is disagreed")
        return self
