"""Tests for rate limiting (429) and transient failure retries (Phase 5)."""

import re

import httpx
import pytest

try:
    import respx
except ImportError:
    respx = None

from code_review.providers.gitea import GiteaProvider


@pytest.mark.skipif(respx is None, reason="respx required")
@pytest.mark.respx(assert_all_mocked=True, assert_all_called=False)
def test_gitea_retries_on_429(respx_mock):
    """Provider retries once on 429 and succeeds on second attempt."""
    url_pattern = re.compile(r"^http://gitea\.test/api/v1/repos/o/r/pulls/1\.diff$")
    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, text="Too Many Requests")
        return httpx.Response(200, text="diff --git a/foo b/foo")

    respx_mock.get(url_pattern).mock(side_effect=side_effect)

    provider = GiteaProvider(base_url="http://gitea.test", token="x")
    result = provider.get_pr_diff("o", "r", 1)
    assert result == "diff --git a/foo b/foo"
    assert call_count == 2


@pytest.mark.skipif(respx is None, reason="respx required")
@pytest.mark.respx(assert_all_mocked=True, assert_all_called=False)
def test_gitea_retries_on_503(respx_mock):
    """Provider retries once on 503 and succeeds on second attempt."""
    url_pattern = re.compile(r"^http://gitea\.test/api/v1/repos/o/r/pulls/1\.diff$")
    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, text="diff content")

    respx_mock.get(url_pattern).mock(side_effect=side_effect)

    provider = GiteaProvider(base_url="http://gitea.test", token="x")
    result = provider.get_pr_diff("o", "r", 1)
    assert result == "diff content"
    assert call_count == 2
