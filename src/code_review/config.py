"""Validated configuration (Pydantic Settings). Centralizes env var handling."""

from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SCM_CONFIG: "SCMConfig | None" = None
_LLM_CONFIG: "LLMConfig | None" = None


class SCMConfig(BaseSettings):
    """SCM (Source Control) configuration."""

    model_config = SettingsConfigDict(
        env_prefix="SCM_",
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    provider: Literal["gitea", "github", "gitlab", "bitbucket", "bitbucket_server"] = "gitea"
    url: str = Field(..., description="API base URL (may differ from UI for self-hosted)")
    token: SecretStr = Field(..., description="API token for authentication")
    owner: str = Field(default="", description="Repo owner/org")
    repo: str = Field(default="", description="Repo name")
    pr_num: int | None = Field(default=None, description="PR/MR number")
    head_sha: str = Field(default="", description="Head commit SHA")
    base_sha: str = Field(default="", description="Base commit SHA")
    event: str = Field(default="", description="Webhook event: opened/synchronize/reopened")
    skip_label: str = Field(
        default="skip-review",
        description="If PR has this label, skip review (empty to disable)",
    )
    skip_title_pattern: str = Field(
        default="[skip-review]",
        description="If PR title contains this substring, skip review (empty to disable)",
    )
    allowed_hosts: str | None = Field(
        default=None,
        description=(
            "Optional comma-separated allowlist of SCM hosts (host[:port]). When set, "
            "SCM_URL must use one of these hosts."
        ),
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("SCM_URL must be a valid http(s) URL with non-empty host")
        return v

    @field_validator("allowed_hosts")
    @classmethod
    def _normalize_allowed_hosts(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = ",".join(h.strip() for h in v.split(",") if h.strip())
        return cleaned or None


class LLMConfig(BaseSettings):
    """LLM configuration."""

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    provider: Literal["gemini", "openai", "anthropic", "ollama", "vertex"] = "gemini"
    model: str = "gemini-2.5-flash"
    context_window: int = Field(
        default=128_000,
        description="Context window in tokens (explicit, avoid model-name guessing)",
    )
    max_output_tokens: int = Field(default=4096, description="Max output tokens")
    temperature: float = Field(default=0.0, description="0 or very low for deterministic review")
    disable_tool_calls: bool = Field(
        default=False,
        description="Disable tool calls for debugging runner logic",
    )
    timeout_seconds: float = Field(
        default=60.0,
        description=(
            "Per-request timeout for LLM API calls. "
            "NOTE: currently configuration-only; see IMPROVEMENT_PLAN "
            "§2.4/§5.5 before relying on it."
        ),
    )
    max_retries: int = Field(
        default=3,
        description=(
            "Max retries on transient LLM failures. "
            "NOTE: currently configuration-only; see IMPROVEMENT_PLAN "
            "§2.4/§5.5 before relying on it."
        ),
    )


def get_scm_config() -> SCMConfig:
    """Return cached SCM config instance."""
    global _SCM_CONFIG
    if _SCM_CONFIG is None:
        _SCM_CONFIG = SCMConfig()
    return _SCM_CONFIG


def get_llm_config() -> LLMConfig:
    """Return cached LLM config instance."""
    global _LLM_CONFIG
    if _LLM_CONFIG is None:
        _LLM_CONFIG = LLMConfig()
    return _LLM_CONFIG


def reset_config_cache() -> None:
    """Reset cached config instances. Intended for use in tests."""
    global _SCM_CONFIG, _LLM_CONFIG
    _SCM_CONFIG = None
    _LLM_CONFIG = None
