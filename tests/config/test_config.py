"""Tests for config module: validators, getters, cache."""

import logging
import os
from unittest.mock import patch

import pytest

from code_review.config import (
    CodeReviewAppConfig,
    ContextAwareReviewConfig,
    LLMConfig,
    SCMConfig,
    SummaryLLMConfig,
    VerificationLLMConfig,
    format_startup_config_lines,
    get_llm_config,
    get_scm_config,
    get_summary_llm_config,
    get_verification_llm_config,
    log_startup_configuration,
    reset_config_cache,
    startup_config_snapshot,
)
from code_review.orchestration.orchestrator import ReviewOrchestrator


def test_scm_config_invalid_url_raises():
    """SCM_URL must be http(s) with non-empty host."""
    with patch.dict(os.environ, {"SCM_URL": "ftps://host", "SCM_TOKEN": "x"}, clear=False):
        with pytest.raises(ValueError, match="SCM_URL must be a valid"):
            SCMConfig()
    with patch.dict(os.environ, {"SCM_URL": "https://", "SCM_TOKEN": "x"}, clear=False):
        with pytest.raises(ValueError, match="SCM_URL must be a valid"):
            SCMConfig()


def test_scm_config_bitbucket_server_user_slug_stripped():
    """Whitespace-only slug is empty so truthy checks match review-decision availability."""
    with patch.dict(
        os.environ,
        {
            "SCM_URL": "https://gitea.example.com",
            "SCM_TOKEN": "x",
            "SCM_BOT_IDENTITY": "",
            "SCM_BITBUCKET_SERVER_USER_SLUG": "  \t  ",
        },
        clear=False,
    ):
        cfg = SCMConfig()
        assert cfg.bitbucket_server_user_slug == ""
        assert cfg.bot_identity == ""
    with patch.dict(
        os.environ,
        {
            "SCM_URL": "https://gitea.example.com",
            "SCM_TOKEN": "x",
            "SCM_BOT_IDENTITY": "",
            "SCM_BITBUCKET_SERVER_USER_SLUG": "  buildbot  ",
        },
        clear=False,
    ):
        cfg = SCMConfig()
        assert cfg.bitbucket_server_user_slug == "buildbot"
        assert cfg.bot_identity == "buildbot"


def test_scm_config_allowed_hosts_normalized():
    """allowed_hosts is stripped and empty segments removed; empty string becomes None."""
    with patch.dict(
        os.environ,
        {
            "SCM_URL": "https://gitea.example.com",
            "SCM_TOKEN": "x",
            "SCM_ALLOWED_HOSTS": "  a , , b  ",
        },
        clear=False,
    ):
        cfg = SCMConfig()
        assert cfg.allowed_hosts == "a,b"
    with patch.dict(
        os.environ,
        {"SCM_URL": "https://gitea.example.com", "SCM_TOKEN": "x", "SCM_ALLOWED_HOSTS": "  "},
        clear=False,
    ):
        cfg = SCMConfig()
        assert cfg.allowed_hosts is None


def test_get_scm_config_caches():
    """get_scm_config returns the same instance on repeated calls."""
    reset_config_cache()
    with patch.dict(
        os.environ,
        {"SCM_URL": "https://gitea.example.com", "SCM_TOKEN": "secret"},
        clear=False,
    ):
        a = get_scm_config()
        b = get_scm_config()
        assert a is b
    reset_config_cache()


def test_get_llm_config_caches():
    """get_llm_config returns the same instance on repeated calls."""
    reset_config_cache()
    with patch.dict(os.environ, {"SCM_URL": "https://x.com", "SCM_TOKEN": "x"}, clear=False):
        pass  # ensure SCM not required for get_llm_config
    with patch.dict(os.environ, {}, clear=False):
        # LLMConfig has defaults; may still read SCM_ from env in same process
        a = get_llm_config()
        b = get_llm_config()
        assert a is b
    reset_config_cache()


def test_llm_config_blank_api_key_normalized_to_none():
    """Blank LLM_API_KEY should be treated as unset, not as an empty secret."""
    with patch.dict(os.environ, {"LLM_API_KEY": "   "}, clear=False):
        cfg = LLMConfig()
        assert cfg.api_key is None


def test_summary_llm_config_reads_optional_overrides():
    with patch.dict(
        os.environ,
        {
            "LLM_SUMMARY_PROVIDER": "gemini",
            "LLM_SUMMARY_MODEL": "gemini-3-flash-lite-preview",
            "LLM_SUMMARY_API_KEY": "  summary-key  ",
        },
        clear=True,
    ):
        cfg = SummaryLLMConfig()
        assert cfg.provider == "gemini"
        assert cfg.model == "gemini-3-flash-lite-preview"
        assert cfg.api_key is not None
        assert cfg.api_key.get_secret_value() == "summary-key"


def test_verification_llm_config_reads_optional_overrides():
    with patch.dict(
        os.environ,
        {
            "LLM_VERIFICATION_PROVIDER": "openai",
            "LLM_VERIFICATION_MODEL": "gpt-5-mini",
            "LLM_VERIFICATION_API_KEY": "  verification-key  ",
        },
        clear=True,
    ):
        cfg = VerificationLLMConfig()
        assert cfg.provider == "openai"
        assert cfg.model == "gpt-5-mini"
        assert cfg.api_key is not None
        assert cfg.api_key.get_secret_value() == "verification-key"


def test_task_llm_config_blank_values_are_unset():
    with patch.dict(
        os.environ,
        {
            "LLM_SUMMARY_PROVIDER": "   ",
            "LLM_SUMMARY_MODEL": "   ",
            "LLM_SUMMARY_API_KEY": "   ",
            "LLM_VERIFICATION_PROVIDER": "   ",
            "LLM_VERIFICATION_MODEL": "   ",
            "LLM_VERIFICATION_API_KEY": "   ",
        },
        clear=True,
    ):
        assert SummaryLLMConfig().provider is None
        assert SummaryLLMConfig().model is None
        assert SummaryLLMConfig().api_key is None
        assert VerificationLLMConfig().provider is None
        assert VerificationLLMConfig().model is None
        assert VerificationLLMConfig().api_key is None


def test_reset_config_cache_clears_both():
    """reset_config_cache() clears SCM and LLM caches so next get_* creates new instances."""
    reset_config_cache()
    with patch.dict(
        os.environ,
        {"SCM_URL": "https://gitea.example.com", "SCM_TOKEN": "secret"},
        clear=False,
    ):
        scm1 = get_scm_config()
    with patch.dict(os.environ, {}, clear=False):
        llm1 = get_llm_config()
        summary1 = get_summary_llm_config()
        verification1 = get_verification_llm_config()
    reset_config_cache()
    with patch.dict(
        os.environ,
        {"SCM_URL": "https://gitea.example.com", "SCM_TOKEN": "secret"},
        clear=False,
    ):
        scm2 = get_scm_config()
    with patch.dict(os.environ, {}, clear=False):
        llm2 = get_llm_config()
        summary2 = get_summary_llm_config()
        verification2 = get_verification_llm_config()
    assert scm1 is not scm2
    assert llm1 is not llm2
    assert summary1 is not summary2
    assert verification1 is not verification2
    reset_config_cache()


def test_scm_review_decision_settings_from_env():
    with patch.dict(
        os.environ,
        {
            "SCM_URL": "https://gitea.example.com",
            "SCM_TOKEN": "secret",
            "SCM_REVIEW_DECISION_ENABLED": "true",
            "SCM_REVIEW_DECISION_HIGH_THRESHOLD": "2",
            "SCM_REVIEW_DECISION_MEDIUM_THRESHOLD": "5",
        },
        clear=False,
    ):
        cfg = SCMConfig()
        assert cfg.review_decision_enabled is True
        assert cfg.review_decision_high_threshold == 2
        assert cfg.review_decision_medium_threshold == 5


def test_review_decision_cli_overrides_use_copy_not_cached_mutation():
    """run_review decision kwargs must apply even if get_scm_config() was already cached."""
    reset_config_cache()
    with patch.dict(
        os.environ,
        {
            "SCM_URL": "https://gitea.example.com",
            "SCM_TOKEN": "secret",
            "SCM_REVIEW_DECISION_ENABLED": "false",
        },
        clear=False,
    ):
        cached = get_scm_config()
        assert cached.review_decision_enabled is False
        orch = ReviewOrchestrator(
            "o",
            "r",
            1,
            head_sha="abc",
            dry_run=True,
            review_decision_enabled=True,
            review_decision_high_threshold=9,
        )
        cfg, _, _ = orch._load_config_and_provider()
        assert cfg.review_decision_enabled is True
        assert cfg.review_decision_high_threshold == 9
        assert get_scm_config() is cached
        assert get_scm_config().review_decision_enabled is False
    reset_config_cache()


def test_startup_config_snapshot_logs_models_and_redacts_secrets():
    reset_config_cache()
    with patch.dict(
        os.environ,
        {
            "SCM_URL": "https://gitea.example.com/api",
            "SCM_TOKEN": "super-secret-scm",
            "SCM_REVIEW_DECISION_ENABLED": "true",
            "LLM_PROVIDER": "gemini",
            "LLM_MODEL": "gemini-3.1-pro-preview",
            "LLM_API_KEY": "super-secret-llm",
            "LLM_SUMMARY_PROVIDER": "anthropic",
            "LLM_SUMMARY_MODEL": "claude-sonnet-4-5",
            "LLM_SUMMARY_API_KEY": "super-secret-summary",
            "CONTEXT_AWARE_REVIEW_ENABLED": "true",
            "CONTEXT_JIRA_ENABLED": "true",
            "CONTEXT_ATLASSIAN_URL": "https://acme.atlassian.net",
            "CONTEXT_ATLASSIAN_TOKEN": "super-secret-atlassian",
        },
        clear=True,
    ):
        snapshot = startup_config_snapshot()
    reset_config_cache()

    assert snapshot["llm"]["primary"] == {
        "provider": "gemini",
        "model": "gemini-3.1-pro-preview",
    }
    assert snapshot["llm"]["summary"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
    }
    assert snapshot["llm"]["verification"] == {
        "provider": "gemini",
        "model": "gemini-3.1-pro-preview",
    }
    assert isinstance(snapshot["llm"]["max_output_tokens"], int)
    assert isinstance(snapshot["llm"]["context_window"], int)
    assert snapshot["context_aware"]["enabled"] is True
    assert snapshot["context_aware"]["jira_enabled"] is True
    assert snapshot["scm"]["provider"] == "gitea"
    assert "url_host" not in snapshot["scm"]
    assert "bot_identity_configured" not in snapshot["scm"]
    rendered = str(snapshot)
    assert "super-secret" not in rendered
    assert "api_key" not in rendered
    assert "SCM_TOKEN" not in rendered
    assert "LLM_API_KEY" not in rendered
    assert "CONTEXT_ATLASSIAN_TOKEN" not in rendered


def test_context_aware_config_uses_shared_atlassian_credentials():
    with patch.dict(
        os.environ,
        {
            "CONTEXT_ATLASSIAN_URL": " https://acme.atlassian.net/ ",
            "CONTEXT_ATLASSIAN_EMAIL": "review-bot@example.com",
            "CONTEXT_ATLASSIAN_TOKEN": "atlassian-token",
        },
        clear=True,
    ):
        cfg = ContextAwareReviewConfig()

    assert cfg.atlassian_url == "https://acme.atlassian.net"
    assert cfg.atlassian_email == "review-bot@example.com"
    assert cfg.atlassian_token is not None
    assert cfg.atlassian_token.get_secret_value() == "atlassian-token"


def test_context_aware_config_normalizes_atlassian_url_to_site_root():
    with patch.dict(
        os.environ,
        {"CONTEXT_ATLASSIAN_URL": "https://acme.atlassian.net/jira/"},
        clear=True,
    ):
        jira_cfg = ContextAwareReviewConfig()
    with patch.dict(
        os.environ,
        {"CONTEXT_ATLASSIAN_URL": "https://acme.atlassian.net/wiki/"},
        clear=True,
    ):
        confluence_cfg = ContextAwareReviewConfig()

    assert jira_cfg.atlassian_url == "https://acme.atlassian.net"
    assert confluence_cfg.atlassian_url == "https://acme.atlassian.net"


def test_format_startup_config_lines_flattens_snapshot():
    lines = format_startup_config_lines(
        {
            "llm": {"primary": {"provider": "openai", "model": "gpt-5.4"}},
            "review": {"review_visible_lines": False},
        }
    )

    assert lines == [
        "Viper startup configuration:",
        "llm.primary.provider: openai",
        "llm.primary.model: gpt-5.4",
        "review.review_visible_lines: False",
    ]


def test_log_startup_configuration_uses_supplied_logger():
    fake_logger = type(
        "FakeLogger",
        (),
        {
            "isEnabledFor": lambda self, level: True,
            "info": lambda self, *args: self.calls.append(args),
        },
    )()
    fake_logger.calls = []

    with patch(
        "code_review.config.startup_config_snapshot",
        return_value={"llm": {"primary": {"model": "gpt-5.4"}}},
    ):
        log_startup_configuration(fake_logger)

    assert fake_logger.calls == [
        ("Viper startup configuration:",),
        ("llm.primary.model: gpt-5.4",),
    ]


def test_log_startup_configuration_logs_at_effective_level_when_info_disabled():
    fake_logger = type(
        "FakeLogger",
        (),
        {
            "isEnabledFor": lambda self, level: False,
            "getEffectiveLevel": lambda self: logging.WARNING,
            "log": lambda self, *args: self.calls.append(args),
        },
    )()
    fake_logger.calls = []

    with patch(
        "code_review.config.startup_config_snapshot",
        return_value={"llm": {"primary": {"model": "gpt-5.4"}}},
    ):
        log_startup_configuration(fake_logger)

    assert fake_logger.calls == [
        (logging.WARNING, "Viper startup configuration:"),
        (logging.WARNING, "llm.primary.model: gpt-5.4"),
    ]


def test_log_startup_configuration_continues_when_snapshot_fails():
    fake_logger = type(
        "FakeLogger",
        (),
        {"warning": lambda self, *args: self.calls.append(args)},
    )()
    fake_logger.calls = []

    with patch("code_review.config.startup_config_snapshot", side_effect=ValueError("bad")):
        log_startup_configuration(fake_logger)

    assert fake_logger.calls == [
        ("Viper startup configuration unavailable: %s", "ValueError"),
    ]


def test_code_review_app_review_decision_only_from_env():
    with patch.dict(os.environ, {"CODE_REVIEW_REVIEW_DECISION_ONLY": "true"}, clear=True):
        cfg = CodeReviewAppConfig()
        assert cfg.review_decision_only is True


def test_code_review_app_review_visible_lines_from_env():
    with patch.dict(os.environ, {"CODE_REVIEW_REVIEW_VISIBLE_LINES": "true"}, clear=True):
        cfg = CodeReviewAppConfig()
        assert cfg.review_visible_lines is True


def test_code_review_app_skip_if_bot_not_blocking_from_env():
    with patch.dict(
        os.environ,
        {"CODE_REVIEW_REVIEW_DECISION_ONLY_SKIP_IF_BOT_NOT_BLOCKING": "true"},
        clear=True,
    ):
        cfg = CodeReviewAppConfig()
        assert cfg.review_decision_only_skip_if_bot_not_blocking is True


def test_code_review_app_reply_dismissal_enabled_from_env():
    with patch.dict(os.environ, {"CODE_REVIEW_REPLY_DISMISSAL_ENABLED": "true"}, clear=True):
        cfg = CodeReviewAppConfig()
        assert cfg.reply_dismissal_enabled is True


def test_code_review_app_disable_idempotency_from_env():
    with patch.dict(os.environ, {"CODE_REVIEW_DISABLE_IDEMPOTENCY": "true"}, clear=True):
        cfg = CodeReviewAppConfig()
        assert cfg.disable_idempotency is True


def test_code_review_app_log_prompts_from_env():
    with patch.dict(os.environ, {"CODE_REVIEW_LOG_PROMPTS": "true"}, clear=True):
        cfg = CodeReviewAppConfig()
        assert cfg.log_prompts is True


def test_code_review_app_started_review_comment_posted_from_env():
    with patch.dict(
        os.environ,
        {"CODE_REVIEW_STARTED_REVIEW_COMMENT_POSTED": "true"},
        clear=True,
    ):
        cfg = CodeReviewAppConfig()
        assert cfg.started_review_comment_posted is True


def test_code_review_app_reply_dismissal_enabled_by_default():
    with patch.dict(os.environ, {}, clear=True):
        cfg = CodeReviewAppConfig()
        assert cfg.reply_dismissal_enabled is True


def test_code_review_app_review_visible_lines_disabled_by_default():
    with patch.dict(os.environ, {}, clear=True):
        cfg = CodeReviewAppConfig()
        assert cfg.review_visible_lines is False
