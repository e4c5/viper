"""Validated configuration (Pydantic Settings). Centralizes env var handling."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SCMConfig(BaseSettings):
    """SCM (Source Control) configuration."""

    model_config = SettingsConfigDict(env_prefix="SCM_", extra="ignore")

    provider: Literal["gitea", "gitlab", "bitbucket"] = "gitea"
    url: str = Field(..., description="API base URL (may differ from UI for self-hosted)")
    token: str = Field(..., description="API token for authentication")
    owner: str = Field(default="", description="Repo owner/org")
    repo: str = Field(default="", description="Repo name")
    pr_num: int | None = Field(default=None, description="PR/MR number")
    head_sha: str = Field(default="", description="Head commit SHA")
    base_sha: str = Field(default="", description="Base commit SHA")
    event: str = Field(default="", description="Webhook event: opened/synchronize/reopened")


class LLMConfig(BaseSettings):
    """LLM configuration."""

    model_config = SettingsConfigDict(env_prefix="LLM_", extra="ignore")

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


@lru_cache
def get_scm_config() -> SCMConfig:
    """Cached SCM config."""
    return SCMConfig()


@lru_cache
def get_llm_config() -> LLMConfig:
    """Cached LLM config."""
    return LLMConfig()
