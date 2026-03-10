"""Tests for BitbucketServerProvider (mocked HTTP)."""

import pytest
from unittest.mock import MagicMock, patch

from code_review.providers import get_provider
from code_review.providers.bitbucket_server import BitbucketServerProvider, _extract_commit_id


def test_get_provider_bitbucket_server():
    p = get_provider("bitbucket_server", "http://bb:7990/rest/api/1.0", "token")
    assert isinstance(p, BitbucketServerProvider)


# ---------------------------------------------------------------------------
# _extract_commit_id unit tests
# ---------------------------------------------------------------------------


def test_extract_commit_id_string_latestcommit():
    """Bitbucket Server commonly returns latestCommit as a plain string hash."""
    ref = {
        "id": "refs/heads/feature/my-branch",
        "displayId": "feature/my-branch",
        "latestCommit": "abc123def456abc123def456abc123def456abc123",
    }
    assert _extract_commit_id(ref) == "abc123def456abc123def456abc123def456abc123"


def test_extract_commit_id_dict_latestcommit():
    """Fall back to latestCommit.id when latestCommit is a dict (older API variants)."""
    ref = {
        "id": "refs/heads/main",
        "latestCommit": {"id": "deadbeef1234"},
    }
    assert _extract_commit_id(ref) == "deadbeef1234"


def test_extract_commit_id_missing_latestcommit_uses_ref_id():
    """Fall back to the ref's own id when latestCommit is absent."""
    ref = {"id": "refs/heads/main"}
    assert _extract_commit_id(ref) == "refs/heads/main"


@pytest.mark.parametrize("bad_latest", [None, ""])
def test_extract_commit_id_empty_latestcommit_uses_ref_id(bad_latest):
    """latestCommit=None/empty falls back to ref.id."""
    assert _extract_commit_id({"id": "refs/heads/main", "latestCommit": bad_latest}) == "refs/heads/main"


# ---------------------------------------------------------------------------
# _get_pr_diff_refs integration tests
# ---------------------------------------------------------------------------


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_pr_diff_refs_string_latest_commit(mock_client):
    """_get_pr_diff_refs works when the Bitbucket Server API returns latestCommit as a string."""
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "fromRef": {
            "id": "refs/heads/feature",
            "latestCommit": "fromhash111",
        },
        "toRef": {
            "id": "refs/heads/main",
            "latestCommit": "tohash222",
        },
    }
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider("http://bb:7990/rest/api/1.0", "tok")
    from_id, to_id = p._get_pr_diff_refs("PROJ", "my-repo", 42)
    assert from_id == "fromhash111"
    assert to_id == "tohash222"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_pr_diff_refs_returns_none_none_on_error(mock_client):
    """_get_pr_diff_refs returns (None, None) gracefully when the API call fails."""
    mock_client.return_value.__enter__.return_value.get.side_effect = RuntimeError("network error")

    p = BitbucketServerProvider("http://bb:7990/rest/api/1.0", "tok")
    from_id, to_id = p._get_pr_diff_refs("PROJ", "my-repo", 1)
    assert from_id is None
    assert to_id is None
