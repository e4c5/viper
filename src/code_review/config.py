"""Validated configuration (Pydantic Settings). Centralizes env var handling.

See docs/CONFIGURATION-REFERENCE.md for a consolidated list of all environment variables.
"""

from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SCM_CONFIG: "SCMConfig | None" = None
_LLM_CONFIG: "LLMConfig | None" = None
_CONTEXT_AWARE_CONFIG: "ContextAwareReviewConfig | None" = None
_CODE_REVIEW_APP_CONFIG: "CodeReviewAppConfig | None" = None


class SCMConfig(BaseSettings):
    """SCM (Source Control) configuration."""

    # NOTE: The application intentionally does NOT load .env files automatically.
    # All configuration must come from the real environment (process env vars,
    # container/CI settings, etc.). This matches the documented contract in
    # README/AGENTS: users are expected to `export` or `source` values themselves.
    model_config = SettingsConfigDict(env_prefix="SCM_", extra="ignore")

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
    review_decision_enabled: bool = Field(
        default=False,
        description=(
            "Automatically submit a PR review decision based on open findings "
            "(requires provider support)."
        ),
    )
    review_decision_high_threshold: int = Field(
        default=1,
        ge=0,
        description="Request changes when open high-severity findings >= this threshold.",
    )
    review_decision_medium_threshold: int = Field(
        default=3,
        ge=0,
        description="Request changes when open medium-severity findings >= this threshold.",
    )
    allowed_hosts: str | None = Field(
        default=None,
        description=(
            "Optional comma-separated allowlist of SCM hosts (host[:port]). When set, "
            "SCM_URL must use one of these hosts."
        ),
    )
    bitbucket_server_user_slug: str = Field(
        default="",
        description=(
            "Bitbucket Server/DC: username slug of the token user for "
            "`PUT .../pull-requests/{id}/participants/{slug}` when submitting review decisions. "
            "Leading/trailing whitespace is stripped; whitespace-only values are treated as empty."
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

    @field_validator("bitbucket_server_user_slug")
    @classmethod
    def _normalize_bitbucket_server_user_slug(cls, v: str) -> str:
        """Strip so whitespace-only env values do not look like a configured slug."""
        return (v or "").strip()


class LLMConfig(BaseSettings):
    """LLM configuration."""

    # See note above: we do not auto-load .env; only real env vars are used.
    model_config = SettingsConfigDict(env_prefix="LLM_", extra="ignore")

    provider: Literal["gemini", "openai", "anthropic", "ollama", "vertex", "openrouter"] = "gemini"
    api_key: SecretStr | None = Field(
        default=None,
        description=(
            "API key for the configured LLM provider "
            "(single key; provider chosen via LLM_PROVIDER)."
        ),
    )
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

    @field_validator("api_key", mode="before")
    @classmethod
    def _normalize_api_key(cls, v: str | SecretStr | None) -> SecretStr | None:
        """Treat blank API keys as unset and trim accidental surrounding spaces."""
        if v is None:
            return None
        raw = v.get_secret_value() if isinstance(v, SecretStr) else str(v)
        normalized = raw.strip()
        if not normalized:
            return None
        return SecretStr(normalized)


class ContextAwareReviewConfig(BaseSettings):
    """Optional context enrichment (issues, Jira, Confluence).

    See docs/CONTEXT-AWARE-USER-GUIDE.md for environment variables.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    enabled: bool = Field(default=False, validation_alias="CONTEXT_AWARE_REVIEW_ENABLED")
    db_url: str | None = Field(default=None, validation_alias="CONTEXT_AWARE_REVIEW_DB_URL")
    github_issues_enabled: bool = Field(
        default=False, validation_alias="CONTEXT_GITHUB_ISSUES_ENABLED"
    )
    gitlab_issues_enabled: bool = Field(
        default=False, validation_alias="CONTEXT_GITLAB_ISSUES_ENABLED"
    )
    jira_enabled: bool = Field(default=False, validation_alias="CONTEXT_JIRA_ENABLED")
    jira_url: str = Field(default="", validation_alias="CONTEXT_JIRA_URL")
    jira_email: str = Field(default="", validation_alias="CONTEXT_JIRA_EMAIL")
    jira_token: SecretStr | None = Field(default=None, validation_alias="CONTEXT_JIRA_TOKEN")
    confluence_enabled: bool = Field(default=False, validation_alias="CONTEXT_CONFLUENCE_ENABLED")
    confluence_url: str = Field(default="", validation_alias="CONTEXT_CONFLUENCE_URL")
    confluence_email: str = Field(default="", validation_alias="CONTEXT_CONFLUENCE_EMAIL")
    confluence_token: SecretStr | None = Field(
        default=None, validation_alias="CONTEXT_CONFLUENCE_TOKEN"
    )
    max_bytes: int = Field(default=20_000, ge=1024, validation_alias="CONTEXT_MAX_BYTES")
    distilled_max_tokens: int = Field(
        default=4000, ge=256, validation_alias="CONTEXT_DISTILLED_MAX_TOKENS"
    )
    embedding_model: str = Field(
        default="text-embedding-3-small",
        validation_alias="CONTEXT_EMBEDDING_MODEL",
    )
    embedding_dimensions: int = Field(
        default=1536,
        ge=256,
        le=8192,
        validation_alias="CONTEXT_EMBEDDING_DIMENSIONS",
    )
    github_api_url: str = Field(
        default="",
        validation_alias="CONTEXT_GITHUB_API_URL",
        description="Override GitHub API base when SCM is not github (e.g. Enterprise API root).",
    )
    github_token: SecretStr | None = Field(
        default=None,
        validation_alias="CONTEXT_GITHUB_TOKEN",
        description="Token for GitHub Issues API when SCM_PROVIDER is not github.",
    )
    gitlab_api_url: str = Field(
        default="",
        validation_alias="CONTEXT_GITLAB_API_URL",
        description="Override GitLab API base when SCM is not gitlab.",
    )
    gitlab_token: SecretStr | None = Field(
        default=None,
        validation_alias="CONTEXT_GITLAB_TOKEN",
        description="Token for GitLab Issues API when SCM_PROVIDER is not gitlab.",
    )
    jira_extra_fields: str = Field(
        default="",
        validation_alias="CONTEXT_JIRA_EXTRA_FIELDS",
        description=(
            "Comma-separated list of additional Jira field names to fetch "
            "(e.g. 'customfield_10016,customfield_10014' for acceptance criteria). "
            "Values are appended to the distillation context."
        ),
    )

    @field_validator(
        "jira_token",
        "confluence_token",
        "github_token",
        "gitlab_token",
        mode="before",
    )
    @classmethod
    def _normalize_optional_secrets(cls, v: str | SecretStr | None) -> SecretStr | None:
        if v is None:
            return None
        raw = v.get_secret_value() if isinstance(v, SecretStr) else str(v)
        normalized = raw.strip()
        if not normalized:
            return None
        return SecretStr(normalized)

    @field_validator("db_url", mode="before")
    @classmethod
    def _normalize_optional_db_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized = str(v).strip()
        return normalized or None

    @field_validator("jira_url", "confluence_url", "gitlab_api_url", mode="after")
    @classmethod
    def _strip_urls(cls, v: str) -> str:
        return (v or "").strip().rstrip("/")


class CodeReviewAppConfig(BaseSettings):
    """Runner-level options not tied to SCM/LLM prefixes."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    include_commit_messages_in_prompt: bool = Field(
        default=True,
        validation_alias="CODE_REVIEW_INCLUDE_COMMIT_MESSAGES_IN_PROMPT",
    )
    review_decision_only: bool = Field(
        default=False,
        validation_alias="CODE_REVIEW_REVIEW_DECISION_ONLY",
        description=(
            "Skip the LLM and inline posting; only recompute quality-gate counts and "
            "submit PR review decision when SCM_REVIEW_DECISION_ENABLED is true. "
            "Optional CODE_REVIEW_EVENT_* env vars attach webhook context for logging "
            "(see docs/CONFIGURATION-REFERENCE.md)."
        ),
    )
    review_decision_only_skip_if_bot_not_blocking: bool = Field(
        default=False,
        validation_alias="CODE_REVIEW_REVIEW_DECISION_ONLY_SKIP_IF_BOT_NOT_BLOCKING",
        description=(
            "Review-decision-only: when the event is reply_added "
            "(non-empty CODE_REVIEW_EVENT_*), skip recomputation if the provider reports "
            "the token user is not in a blocking review state. "
            "No effect when event context is empty or for non-reply events."
        ),
    )
    reply_dismissal_enabled: bool = Field(
        default=True,
        validation_alias="CODE_REVIEW_REPLY_DISMISSAL_ENABLED",
        description=(
            "Review-decision-only: when event_kind is reply_added and CODE_REVIEW_EVENT_COMMENT_ID "
            "is set, run the reply-dismissal LLM on the thread (GitHub/GitLab when supported); "
            "if agreed, exclude that thread from quality-gate counts; if disagreed, optionally "
            "post a thread reply when the provider supports it."
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


def get_context_aware_config() -> ContextAwareReviewConfig:
    """Return cached context-aware review config."""
    global _CONTEXT_AWARE_CONFIG
    if _CONTEXT_AWARE_CONFIG is None:
        _CONTEXT_AWARE_CONFIG = ContextAwareReviewConfig()
    return _CONTEXT_AWARE_CONFIG


def get_code_review_app_config() -> CodeReviewAppConfig:
    """Return cached app-level review config (prompt toggles, etc.)."""
    global _CODE_REVIEW_APP_CONFIG
    if _CODE_REVIEW_APP_CONFIG is None:
        _CODE_REVIEW_APP_CONFIG = CodeReviewAppConfig()
    return _CODE_REVIEW_APP_CONFIG


def reset_config_cache() -> None:
    """Reset cached config instances. Intended for use in tests."""
    global _SCM_CONFIG, _LLM_CONFIG, _CONTEXT_AWARE_CONFIG, _CODE_REVIEW_APP_CONFIG
    _SCM_CONFIG = None
    _LLM_CONFIG = None
    _CONTEXT_AWARE_CONFIG = None
    _CODE_REVIEW_APP_CONFIG = None
