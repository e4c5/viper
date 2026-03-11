"""Tests for BitbucketServerProvider (mocked HTTP)."""

import pytest
from unittest.mock import MagicMock, call, patch

from code_review.providers import get_provider
from code_review.providers.base import InlineComment
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


# ---------------------------------------------------------------------------
# post_review_comments — lineType correctness
# ---------------------------------------------------------------------------


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_post_review_comments_uses_line_type_added(mock_client):
    """ADDED lines must be posted with lineType='ADDED'."""
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = {"id": 1}
    mock_get = MagicMock()
    mock_get.headers = {"content-type": "application/json"}
    mock_get.json.return_value = {
        "fromRef": {"latestCommit": "fromhash"},
        "toRef": {"latestCommit": "tohash"},
    }
    http = mock_client.return_value.__enter__.return_value
    http.get.return_value = mock_get
    http.post.return_value = mock_post

    p = BitbucketServerProvider("http://bb:7990/rest/api/1.0", "tok")
    p.post_review_comments(
        "PROJ", "repo", 1,
        [InlineComment(path="foo.java", line=10, body="Bug", line_type="ADDED")],
        head_sha="sha1",
    )
    payload = http.post.call_args[1]["json"]
    assert payload["anchor"]["lineType"] == "ADDED"
    assert payload["anchor"]["line"] == 10


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_post_review_comments_uses_line_type_context(mock_client):
    """CONTEXT lines must be posted with lineType='CONTEXT' to avoid Bitbucket Server 409."""
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = {"id": 1}
    mock_get = MagicMock()
    mock_get.headers = {"content-type": "application/json"}
    mock_get.json.return_value = {
        "fromRef": {"latestCommit": "fromhash"},
        "toRef": {"latestCommit": "tohash"},
    }
    http = mock_client.return_value.__enter__.return_value
    http.get.return_value = mock_get
    http.post.return_value = mock_post

    p = BitbucketServerProvider("http://bb:7990/rest/api/1.0", "tok")
    p.post_review_comments(
        "PROJ", "repo", 1,
        [InlineComment(path="foo.java", line=8, body="Context issue", line_type="CONTEXT")],
        head_sha="sha1",
    )
    payload = http.post.call_args[1]["json"]
    assert payload["anchor"]["lineType"] == "CONTEXT", (
        "Context lines must use lineType='CONTEXT'; sending 'ADDED' causes Bitbucket Server 409"
    )


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_post_review_comments_uses_base_to_head_hash_direction_for_to_file(mock_client):
    """For fileType='TO', anchor hashes must be destination/base -> source/head."""
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = {"id": 1}
    mock_get = MagicMock()
    mock_get.headers = {"content-type": "application/json"}
    mock_get.json.return_value = {
        "fromRef": {"latestCommit": "source_head_hash"},
        "toRef": {"latestCommit": "target_base_hash"},
    }
    http = mock_client.return_value.__enter__.return_value
    http.get.return_value = mock_get
    http.post.return_value = mock_post

    p = BitbucketServerProvider("http://bb:7990/rest/api/1.0", "tok")
    p.post_review_comments(
        "PROJ", "repo", 1,
        [InlineComment(path="foo.java", line=10, body="Bug", line_type="ADDED")],
        head_sha="source_head_hash",
    )
    payload = http.post.call_args[1]["json"]
    assert payload["anchor"]["fileType"] == "TO"
    assert payload["anchor"]["fromHash"] == "target_base_hash"
    assert payload["anchor"]["toHash"] == "source_head_hash"


# ---------------------------------------------------------------------------
# _post_comments_one_by_one — fallback preserves line_type
# ---------------------------------------------------------------------------


def test_fallback_preserves_line_type_for_bitbucket_server():
    """_post_comments_one_by_one must call post_review_comments([c]) not post_review_comment().

    post_review_comment() (base class) reconstructs InlineComment without line_type,
    causing Bitbucket Server to default to lineType='ADDED' for all lines.  For CONTEXT
    lines this results in HTTP 409 because the lineType doesn't match the diff line.
    The fix is to call post_review_comments([c]) which passes the full InlineComment.
    """
    from code_review.runner import _post_comments_one_by_one

    provider = MagicMock()
    provider.post_review_comments = MagicMock()

    context_comment = InlineComment(path="foo.java", line=8, body="Issue", line_type="CONTEXT")
    added_comment = InlineComment(path="foo.java", line=10, body="Bug", line_type="ADDED")

    _post_comments_one_by_one(provider, "PROJ", "repo", 1, "sha1", [context_comment, added_comment])

    # Must use post_review_comments (not post_review_comment) so line_type is preserved
    assert provider.post_review_comments.call_count == 2
    assert provider.post_review_comment.call_count == 0, (
        "post_review_comment() must not be called in the fallback path — it strips line_type"
    )

    # Verify the exact InlineComment objects are passed (preserving line_type)
    calls = provider.post_review_comments.call_args_list
    first_call_comments = calls[0][0][3]  # positional arg: comments list
    assert first_call_comments[0].line_type == "CONTEXT"
    second_call_comments = calls[1][0][3]
    assert second_call_comments[0].line_type == "ADDED"


def test_fallback_no_pr_summary_when_inline_fails():
    """_post_comments_one_by_one must NOT call post_pr_summary_comment as a fallback.

    When individual inline posting fails, the comment is simply skipped (logged as WARNING).
    This mirrors the tool-based (file-by-file / multi-shot) behaviour.
    """
    from code_review.runner import _post_comments_one_by_one

    provider = MagicMock()
    provider.post_review_comments.side_effect = RuntimeError("409 Conflict")
    provider.post_pr_summary_comment = MagicMock()

    comment = InlineComment(path="foo.java", line=8, body="Issue", line_type="CONTEXT")
    count = _post_comments_one_by_one(provider, "PROJ", "repo", 1, "sha1", [comment])

    # Nothing posted successfully
    assert count == 0
    # PR summary fallback must NOT be called
    provider.post_pr_summary_comment.assert_not_called()
