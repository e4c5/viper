"""Validated configuration (Pydantic Settings). Centralizes env var handling.

See docs/CONFIGURATION-REFERENCE.md for a consolidated list of all environment variables.
"""

import logging
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SCM_CONFIG: "SCMConfig | None" = None
_LLM_CONFIG: "LLMConfig | None" = None
_SUMMARY_LLM_CONFIG: "TaskLLMConfig | None" = None
_VERIFICATION_LLM_CONFIG: "TaskLLMConfig | None" = None
_CONTEXT_AWARE_CONFIG: "ContextAwareReviewConfig | None" = None
_CODE_REVIEW_APP_CONFIG: "CodeReviewAppConfig | None" = None
logger = logging.getLogger(__name__)


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
    bot_identity: str = Field(
        default="",
        description=(
            "The bot account's login/slug, used to attribute idempotency checks "
            "and quality-gate comment filtering back to the bot."
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

    @model_validator(mode="after")
    def _apply_bot_identity_fallback(self) -> "SCMConfig":
        if not self.bot_identity and self.bitbucket_server_user_slug:
            self.bot_identity = self.bitbucket_server_user_slug
        return self


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
    model: str = "gemini-3.1"
    context_window: int = Field(
        default=128_000,
        description="Context window in tokens (explicit, avoid model-name guessing)",
    )
    max_output_tokens: int = Field(default=4096, description="Max output tokens")
    temperature: float = Field(default=0.0, description="0 or very low for deterministic review")
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


class TaskLLMConfig(BaseSettings):
    """Optional task-specific LLM overrides.

    Blank values are treated as unset so task-specific configs can fall back to
    the primary ``LLM_*`` settings field by field.
    """

    model_config = SettingsConfigDict(extra="ignore")

    provider: Literal["gemini", "openai", "anthropic", "ollama", "vertex", "openrouter"] | None = (
        None
    )
    api_key: SecretStr | None = None
    model: str | None = None

    @field_validator("provider", "model", mode="before")
    @classmethod
    def _normalize_string_field(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized = str(v).strip()
        return normalized or None

    @field_validator("api_key", mode="before")
    @classmethod
    def _normalize_api_key(cls, v: str | SecretStr | None) -> SecretStr | None:
        if v is None:
            return None
        raw = v.get_secret_value() if isinstance(v, SecretStr) else str(v)
        normalized = raw.strip()
        if not normalized:
            return None
        return SecretStr(normalized)

class SummaryLLMConfig(TaskLLMConfig):
    """Optional LLM overrides for PR summary generation."""

    model_config = SettingsConfigDict(env_prefix="LLM_SUMMARY_", extra="ignore")


class VerificationLLMConfig(TaskLLMConfig):
    """Optional LLM overrides for finding verification."""

    model_config = SettingsConfigDict(env_prefix="LLM_VERIFICATION_", extra="ignore")


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
    review_visible_lines: bool = Field(
        default=False,
        validation_alias="CODE_REVIEW_REVIEW_VISIBLE_LINES",
        description=(
            "When true, allow findings on any diff-visible new-file line (added + context). "
            "Default false limits findings to changed added lines only."
        ),
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
    log_prompts: bool = Field(
        default=False,
        validation_alias="CODE_REVIEW_LOG_PROMPTS",
        description="Log the assembled LLM instruction and user prompt for debugging.",
    )
    started_review_comment_posted: bool = Field(
        default=False,
        validation_alias="CODE_REVIEW_STARTED_REVIEW_COMMENT_POSTED",
        description=(
            "When true, skip posting the temporary 'review started' PR comment. "
            "Used by CI workflows that already posted the notice before invoking the reviewer."
        ),
    )
    disable_idempotency: bool = Field(
        default=False,
        validation_alias="CODE_REVIEW_DISABLE_IDEMPOTENCY",
        description=(
            "Test-only escape hatch: skip the normal idempotency short-circuit so the "
            "same PR head/config can be reviewed again."
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


def get_summary_llm_config() -> SummaryLLMConfig:
    """Return cached summary LLM override config."""
    global _SUMMARY_LLM_CONFIG
    if _SUMMARY_LLM_CONFIG is None:
        _SUMMARY_LLM_CONFIG = SummaryLLMConfig()
    return _SUMMARY_LLM_CONFIG


def get_verification_llm_config() -> VerificationLLMConfig:
    """Return cached verification LLM override config."""
    global _VERIFICATION_LLM_CONFIG
    if _VERIFICATION_LLM_CONFIG is None:
        _VERIFICATION_LLM_CONFIG = VerificationLLMConfig()
    return _VERIFICATION_LLM_CONFIG


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


def _effective_task_llm(primary: LLMConfig, task: TaskLLMConfig) -> dict[str, str]:
    return {
        "provider": task.provider or primary.provider,
        "model": task.model or primary.model,
    }


def _scm_startup_snapshot() -> dict[str, object]:
    try:
        scm = get_scm_config()
    except Exception as exc:
        return {
            "configured": False,
            "reason": exc.__class__.__name__,
        }
    parsed_url = urlparse(scm.url)
    return {
        "configured": True,
        "provider": scm.provider,
        "url_host": parsed_url.netloc,
        "skip_label_enabled": bool(scm.skip_label),
        "skip_title_pattern_enabled": bool(scm.skip_title_pattern),
        "review_decision_enabled": scm.review_decision_enabled,
        "review_decision_high_threshold": scm.review_decision_high_threshold,
        "review_decision_medium_threshold": scm.review_decision_medium_threshold,
        "allowed_hosts_configured": bool(scm.allowed_hosts),
        "bot_identity_configured": bool(scm.bot_identity),
    }


def startup_config_snapshot() -> dict[str, object]:
    """Return a redacted, curated snapshot of startup-critical configuration."""
    llm = get_llm_config()
    summary_llm = get_summary_llm_config()
    verification_llm = get_verification_llm_config()
    context = get_context_aware_config()
    app = get_code_review_app_config()
    return {
        "llm": {
            "primary": {"provider": llm.provider, "model": llm.model},
            "summary": _effective_task_llm(llm, summary_llm),
            "verification": _effective_task_llm(llm, verification_llm),
            "context_window": llm.context_window,
            "max_output_tokens": llm.max_output_tokens,
            "temperature": llm.temperature,
            "timeout_seconds": llm.timeout_seconds,
            "max_retries": llm.max_retries,
        },
        "review": {
            "include_commit_messages_in_prompt": app.include_commit_messages_in_prompt,
            "review_visible_lines": app.review_visible_lines,
            "review_decision_only": app.review_decision_only,
            "reply_dismissal_enabled": app.reply_dismissal_enabled,
            "disable_idempotency": app.disable_idempotency,
        },
        "context_aware": {
            "enabled": context.enabled,
            "github_issues_enabled": context.github_issues_enabled,
            "gitlab_issues_enabled": context.gitlab_issues_enabled,
            "jira_enabled": context.jira_enabled,
            "confluence_enabled": context.confluence_enabled,
            "db_cache_enabled": bool(context.db_url),
            "max_bytes": context.max_bytes,
            "distilled_max_tokens": context.distilled_max_tokens,
            "embedding_model": context.embedding_model,
            "embedding_dimensions": context.embedding_dimensions,
        },
        "scm": _scm_startup_snapshot(),
    }


def log_startup_configuration(log: logging.Logger | None = None) -> None:
    """Log startup-critical configuration without exposing secrets."""
    target = log or logger
    snapshot = startup_config_snapshot()
    if target.isEnabledFor(logging.INFO):
        target.info("Viper startup configuration: %s", snapshot)
        return
    print(f"Viper startup configuration: {snapshot}", flush=True)


def reset_config_cache() -> None:
    """Reset cached config instances. Intended for use in tests."""
    global _SCM_CONFIG, _LLM_CONFIG, _SUMMARY_LLM_CONFIG, _VERIFICATION_LLM_CONFIG
    global _CONTEXT_AWARE_CONFIG, _CODE_REVIEW_APP_CONFIG
    _SCM_CONFIG = None
    _LLM_CONFIG = None
    _SUMMARY_LLM_CONFIG = None
    _VERIFICATION_LLM_CONFIG = None
    _CONTEXT_AWARE_CONFIG = None
    _CODE_REVIEW_APP_CONFIG = None
