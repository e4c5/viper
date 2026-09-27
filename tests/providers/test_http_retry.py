"""Shared HTTP retry/back-off policy in HttpXProvider._request."""

import re
from unittest.mock import patch

import httpx
import pytest

try:
    import respx
except ImportError:
    respx = None

from code_review.providers.base import RateLimitError
from code_review.providers.gitea import GiteaProvider

pytestmark = pytest.mark.skipif(respx is None, reason="respx required")

_URL = re.compile(r"^http://gitea\.test/api/v1/repos/o/r/pulls/1\.diff$")
POST_URL = re.compile(r"^http://gitea\.test/api/v1/repos/o/r/issues/1/comments$")


def _provider() -> GiteaProvider:
    return GiteaProvider(base_url="http://gitea.test", token="x")


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=False)
def test_get_503_retried_then_200(respx_mock):
    calls = []

    def side_effect(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, text="diff")

    respx_mock.get(_URL).mock(side_effect=side_effect)
    with patch("code_review.providers.http_base._sleep") as sleep:
        assert _provider().get_pr_diff("o", "r", 1) == "diff"
    assert len(calls) == 2
    assert sleep.call_count == 1


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=False)
def test_post_503_not_retried(respx_mock):
    calls = []

    def side_effect(request):
        calls.append(1)
        return httpx.Response(503, text="unavailable")

    respx_mock.post(POST_URL).mock(side_effect=side_effect)
    with pytest.raises(httpx.HTTPStatusError):
        _provider()._request("POST", "/repos/o/r/issues/1/comments", json={"body": "x"})
    assert len(calls) == 1


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=False)
def test_post_connect_error_retried(respx_mock):
    calls = []

    def side_effect(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(201, json={"id": 1})

    respx_mock.post(POST_URL).mock(side_effect=side_effect)
    with patch("code_review.providers.http_base._sleep") as sleep:
        resp = _provider()._request(
            "POST", "/repos/o/r/issues/1/comments", json={"body": "x"}
        )
    assert resp.status_code == 201
    assert len(calls) == 2
    assert sleep.call_count == 1


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=False)
def test_429_retry_after_then_success(respx_mock):
    calls = []

    def side_effect(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")
        return httpx.Response(200, text="diff")

    respx_mock.get(_URL).mock(side_effect=side_effect)
    with patch("code_review.providers.http_base._sleep") as sleep:
        assert _provider().get_pr_diff("o", "r", 1) == "diff"
    sleep.assert_called_once_with(2.0)


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=False)
def test_429_exhausted_raises_rate_limit_error(respx_mock):
    respx_mock.get(_URL).mock(
        return_value=httpx.Response(429, text="slow down")
    )
    provider = _provider()
    with (
        patch("code_review.providers.http_base._sleep"),
        pytest.raises(RateLimitError),
    ):
        provider.get_pr_diff("o", "r", 1)


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=False)
def test_429_retry_after_beyond_cap_raises_immediately(respx_mock):
    respx_mock.get(_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "300"}, text="slow")
    )
    calls = []

    def counted(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "300"}, text="slow")

    respx_mock.get(_URL).mock(side_effect=counted)
    with (
        patch("code_review.providers.http_base._sleep") as sleep,
        pytest.raises(RateLimitError),
    ):
        _provider().get_pr_diff("o", "r", 1)
    sleep.assert_not_called()
    assert len(calls) == 1


@pytest.mark.respx(assert_all_mocked=True, assert_all_called=False)
def test_post_read_timeout_not_retried(respx_mock):
    calls = []

    def side_effect(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    respx_mock.post(POST_URL).mock(side_effect=side_effect)
    with pytest.raises(httpx.ReadTimeout):
        _provider()._request("POST", "/repos/o/r/issues/1/comments", json={})
    assert len(calls) == 1
