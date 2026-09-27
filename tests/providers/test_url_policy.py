"""Tests for the SSRF base-URL policy (providers/url_policy.py) and the
_build_url cross-origin guard in http_base."""

import socket
from unittest.mock import patch

import pytest

from code_review.providers.gitea import GiteaProvider
from code_review.providers.url_policy import validate_scm_base_url


def test_allowed_hosts_exact_match():
    validate_scm_base_url(
        "https://git.example.com/api", allowed_hosts="git.example.com", block_private=False
    )


def test_allowed_hosts_case_insensitive():
    validate_scm_base_url(
        "https://GIT.Example.COM/", allowed_hosts="git.example.com", block_private=False
    )


def test_allowed_hosts_port_match():
    validate_scm_base_url(
        "https://git.example.com:8443/",
        allowed_hosts="git.example.com:8443",
        block_private=False,
    )


def test_allowed_hosts_port_mismatch_rejected():
    with pytest.raises(ValueError, match="not in SCM_ALLOWED_HOSTS"):
        validate_scm_base_url(
            "https://git.example.com:9443/",
            allowed_hosts="git.example.com:8443",
            block_private=False,
        )


def test_allowed_hosts_suffix_matches_subdomain():
    validate_scm_base_url(
        "https://git.example.com/", allowed_hosts=".example.com", block_private=False
    )


def test_allowed_hosts_suffix_does_not_match_apex():
    with pytest.raises(ValueError):
        validate_scm_base_url(
            "https://example.com/", allowed_hosts=".example.com", block_private=False
        )


def test_allowed_hosts_mismatch_rejected():
    with pytest.raises(ValueError):
        validate_scm_base_url(
            "https://evil.example.org/", allowed_hosts="git.example.com", block_private=False
        )


def test_link_local_literal_rejected():
    with pytest.raises(ValueError, match="link-local"):
        validate_scm_base_url(
            "http://169.254.169.254/latest", allowed_hosts=None, block_private=False
        )


def test_hostname_resolving_to_link_local_rejected():
    with patch(
        "code_review.providers.url_policy.socket.getaddrinfo",
        return_value=[(socket.AF_INET, None, None, "", ("169.254.169.254", 80))],
    ):
        with pytest.raises(ValueError, match="link-local"):
            validate_scm_base_url(
                "http://sneaky.example.com/", allowed_hosts=None, block_private=False
            )


def test_metadata_hostname_rejected():
    with pytest.raises(ValueError, match="metadata"):
        validate_scm_base_url(
            "http://metadata.google.internal/", allowed_hosts=None, block_private=False
        )


def test_unresolvable_host_not_rejected():
    with patch(
        "code_review.providers.url_policy.socket.getaddrinfo",
        side_effect=OSError("no dns"),
    ):
        validate_scm_base_url(
            "https://git.internal.example/", allowed_hosts=None, block_private=False
        )


def test_private_ip_allowed_by_default():
    with patch(
        "code_review.providers.url_policy.socket.getaddrinfo",
        return_value=[(socket.AF_INET, None, None, "", ("10.0.0.5", 443))],
    ):
        validate_scm_base_url(
            "https://gitlab.internal/", allowed_hosts=None, block_private=False
        )


def test_private_ip_rejected_when_block_private():
    with patch(
        "code_review.providers.url_policy.socket.getaddrinfo",
        return_value=[(socket.AF_INET, None, None, "", ("10.0.0.5", 443))],
    ):
        with pytest.raises(ValueError, match="private"):
            validate_scm_base_url(
                "https://gitlab.internal/", allowed_hosts=None, block_private=True
            )


def test_cgnat_rejected_when_block_private():
    with patch(
        "code_review.providers.url_policy.socket.getaddrinfo",
        return_value=[(socket.AF_INET, None, None, "", ("100.64.1.1", 443))],
    ):
        with pytest.raises(ValueError):
            validate_scm_base_url(
                "https://x.example/", allowed_hosts=None, block_private=True
            )


def test_orchestrator_rejects_disallowed_host_before_provider_call():
    from code_review.orchestration.orchestrator import ReviewOrchestrator

    orchestrator = ReviewOrchestrator("o", "r", 1, head_sha="abc", dry_run=True)
    cfg = type("C", (), {})()
    cfg.provider = "gitea"
    cfg.url = "https://evil.example.org"
    cfg.token = "x"
    cfg.bot_identity = ""
    cfg.allowed_hosts = "git.example.com"
    cfg.block_private_hosts = False
    orchestrator._scm_config_override = cfg
    orchestrator._llm_config_override = type("L", (), {})()

    with patch(
        "code_review.orchestration_deps.get_provider"
    ) as mock_get_provider:
        with pytest.raises(ValueError, match="SCM_ALLOWED_HOSTS"):
            orchestrator._load_config_and_provider()
        mock_get_provider.assert_not_called()


def test_build_url_refuses_cross_origin():
    provider = GiteaProvider(base_url="https://git.example.com", token="x")
    with pytest.raises(ValueError, match="cross-origin"):
        provider._build_url("https://evil.example.com/api/v1/repos/o/r")


def test_build_url_allows_same_origin_absolute():
    provider = GiteaProvider(base_url="https://git.example.com:8443", token="x")
    assert (
        provider._build_url("https://git.example.com:8443/api/v1/next")
        == "https://git.example.com:8443/api/v1/next"
    )
