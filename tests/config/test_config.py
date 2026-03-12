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
    with patch.dict(os.environ, {"SCM_URL": "ftp://host", "SCM_TOKEN": "x"}, clear=False):
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
