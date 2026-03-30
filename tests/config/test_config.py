"""Tests for config module: validators, getters, cache."""

import os
from unittest.mock import patch

import pytest

from code_review.config import (
    CodeReviewAppConfig,
    LLMConfig,
    SCMConfig,
    get_llm_config,
    get_scm_config,
    reset_config_cache,
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
            "SCM_BITBUCKET_SERVER_USER_SLUG": "  \t  ",
        },
        clear=False,
    ):
        cfg = SCMConfig()
        assert cfg.bitbucket_server_user_slug == ""
    with patch.dict(
        os.environ,
        {
            "SCM_URL": "https://gitea.example.com",
            "SCM_TOKEN": "x",
            "SCM_BITBUCKET_SERVER_USER_SLUG": "  buildbot  ",
        },
        clear=False,
    ):
        cfg = SCMConfig()
        assert cfg.bitbucket_server_user_slug == "buildbot"


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
    reset_config_cache()
    with patch.dict(
        os.environ,
        {"SCM_URL": "https://gitea.example.com", "SCM_TOKEN": "secret"},
        clear=False,
    ):
        scm2 = get_scm_config()
    with patch.dict(os.environ, {}, clear=False):
        llm2 = get_llm_config()
    assert scm1 is not scm2
    assert llm1 is not llm2
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


def test_code_review_app_review_decision_only_from_env():
    with patch.dict(os.environ, {"CODE_REVIEW_REVIEW_DECISION_ONLY": "true"}, clear=True):
        cfg = CodeReviewAppConfig()
        assert cfg.review_decision_only is True


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


def test_code_review_app_reply_dismissal_enabled_by_default():
    with patch.dict(os.environ, {}, clear=True):
        cfg = CodeReviewAppConfig()
        assert cfg.reply_dismissal_enabled is True
