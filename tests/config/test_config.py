"""Tests for config module: validators, getters, cache."""

import os
from unittest.mock import patch

import pytest

from code_review.config import (
    LLMConfig,
    SCMConfig,
    get_llm_config,
    get_scm_config,
    reset_config_cache,
)


def test_scm_config_invalid_url_raises():
    """SCM_URL must be http(s) with non-empty host."""
    with patch.dict(os.environ, {"SCM_URL": "ftps://host", "SCM_TOKEN": "x"}, clear=False):
        with pytest.raises(ValueError, match="SCM_URL must be a valid"):
            SCMConfig()
    with patch.dict(os.environ, {"SCM_URL": "https://", "SCM_TOKEN": "x"}, clear=False):
        with pytest.raises(ValueError, match="SCM_URL must be a valid"):
            SCMConfig()


def test_scm_config_allowed_hosts_normalized():
    """allowed_hosts is stripped and empty segments removed; empty string becomes None."""
    with patch.dict(
        os.environ,
        {"SCM_URL": "https://gitea.example.com", "SCM_TOKEN": "x", "SCM_ALLOWED_HOSTS": "  a , , b  "},
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


def test_context_config_defaults():
    """ContextConfig defaults: disabled, no Jira/Confluence credentials."""
    from code_review.config import ContextConfig, reset_config_cache

    reset_config_cache()
    with patch.dict(os.environ, {}, clear=False):
        cfg = ContextConfig()
    assert cfg.enabled is False
    assert cfg.github_issues_enabled is True
    assert cfg.jira_enabled is False
    assert cfg.confluence_enabled is False
    assert cfg.jira_url is None
    assert cfg.confluence_url is None
    assert cfg.max_context_tokens == 20_000
    reset_config_cache()


def test_context_config_enabled_via_env():
    """CONTEXT_ENABLED=true activates context enrichment."""
    from code_review.config import ContextConfig, reset_config_cache

    reset_config_cache()
    with patch.dict(
        os.environ,
        {
            "CONTEXT_ENABLED": "true",
            "CONTEXT_JIRA_ENABLED": "true",
            "CONTEXT_JIRA_URL": "https://company.atlassian.net",
            "CONTEXT_JIRA_EMAIL": "user@company.com",
            "CONTEXT_JIRA_TOKEN": "secret-token",
            "CONTEXT_JIRA_PROJECT_KEYS": "PROJ,APP",
        },
        clear=False,
    ):
        cfg = ContextConfig()
    assert cfg.enabled is True
    assert cfg.jira_enabled is True
    assert cfg.jira_url == "https://company.atlassian.net"
    assert cfg.jira_email == "user@company.com"
    assert cfg.jira_token is not None
    assert cfg.jira_token.get_secret_value() == "secret-token"
    assert cfg.jira_project_keys == "PROJ,APP"
    reset_config_cache()


def test_get_context_config_cached():
    """get_context_config() returns the same instance on repeated calls."""
    from code_review.config import get_context_config, reset_config_cache

    reset_config_cache()
    cfg1 = get_context_config()
    cfg2 = get_context_config()
    assert cfg1 is cfg2
    reset_config_cache()
