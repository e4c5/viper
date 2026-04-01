"""Pydantic models for code review findings."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class FindingV1(BaseModel):
    """A single code review finding with position and metadata for fingerprinting."""

    version: str = Field(default="1", description="Schema version for output contract")
    path: str = Field(..., description="File path (relative to repo root)")
    line: int = Field(..., ge=1, description="Line number (or start line for ranges)")
    end_line: int | None = Field(
        default=None,
        ge=1,
        description="Optional end line for multi-line findings",
    )
    severity: Literal["high", "medium", "low", "nit"]
    code: str = Field(..., description="Issue code (e.g. unused-var) for fingerprinting")
    message: str = Field(..., description="Human-readable message body")
    body: str | None = Field(
        default=None,
        description="Alias for message; populated from message if unset",
    )
    category: str | None = Field(
        default=None,
        description=("e.g. Correctness, Security, Style; use NeedsVerification for uncertainty"),
    )
    confidence: Literal["high", "medium", "low"] | None = Field(
        default=None,
        description="Optional confidence level for how well the visible code supports the claim",
    )
    evidence: str | None = Field(
        default=None,
        description=(
            "Optional short factual justification citing the visible code "
            "that supports the finding"
        ),
    )
    anchor: str | None = Field(
        default=None,
        description="Optional anchor text for stable positioning when lines shift",
    )
    fingerprint_hint: str | None = Field(
        default=None,
        description="Code span or anchor text to help runner fingerprinting",
    )
    suggested_patch: str | None = Field(
        default=None,
        description=(
            "Optional suggested code change to render as a suggestion block "
            "when provider supports suggestions"
        ),
    )
    agent_fix_prompt: str | None = Field(
        default=None,
        description=(
            "Optional natural-language prompt that a downstream AI coding agent can use "
            "to validate and apply the fix for this finding"
        ),
    )

    @model_validator(mode="after")
    def end_line_not_less_than_line(self) -> "FindingV1":
        if self.end_line is not None and self.end_line < self.line:
            raise ValueError(f"end_line ({self.end_line}) must be >= line ({self.line})")
        return self

    def get_body(self) -> str:
        """Comment body; use body if set else message."""
        return self.body if self.body is not None else self.message


class FindingsBatchV1(BaseModel):
    """Structured ADK output wrapper for a review run's findings."""

    model_config = {"extra": "ignore"}

    findings: list[FindingV1] = Field(
        default_factory=list,
        description=(
            "Structured list of code review findings. "
            "Use an empty list when no issues exist."
        ),
    )
