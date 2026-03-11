"""Tests for Bitbucket provider (mocked HTTP)."""

from unittest.mock import MagicMock, patch

from code_review.providers import get_provider
from code_review.providers.base import InlineComment
from code_review.providers.bitbucket import BitbucketProvider


def test_get_provider_bitbucket():
    p = get_provider("bitbucket", "https://api.bitbucket.org/2.0", "token")
    assert isinstance(p, BitbucketProvider)


@patch("code_review.providers.bitbucket.httpx.Client")
def test_get_pr_diff(mock_client):
    mock_resp = MagicMock()
    mock_resp.text = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"
    mock_resp.headers = {}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    diff = p.get_pr_diff("owner", "repo", 1)
    assert "diff --git" in diff


@patch("code_review.providers.bitbucket.httpx.Client")
def test_get_file_content(mock_client):
    mock_resp = MagicMock()
    mock_resp.text = "print('hello')"
    mock_resp.headers = {}
    mock_resp.content = b"print('hello')"
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    content = p.get_file_content("owner", "repo", "main", "foo.py")
    assert content == "print('hello')"


@patch("code_review.providers.bitbucket.httpx.Client")
def test_get_pr_files(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "values": [
            {"new": {"path": "foo.py"}, "old": {"path": "foo.py"}, "status": "modified"},
            {"new": {"path": "bar.go"}, "old": {"path": "bar.go"}, "status": "added"},
        ]
    }
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    files = p.get_pr_files("owner", "repo", 1)
    assert len(files) == 2
    assert files[0].path == "foo.py"
    assert files[1].path == "bar.go"


@patch("code_review.providers.bitbucket.httpx.Client")
def test_post_review_comments(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = {"id": 1}
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    p.post_review_comments(
        "owner",
        "repo",
        1,
        [InlineComment(path="foo.py", line=10, body="[Critical] Bug here")],
        head_sha="abc123",
    )
    call_args = mock_client.return_value.__enter__.return_value.post.call_args
    assert "comments" in call_args[0][0]
    payload = call_args[1]["json"]
    assert payload["content"]["raw"] == "[Critical] Bug here"
    assert payload["inline"]["path"] == "foo.py"
    assert payload["inline"]["to"] == 10


@patch("code_review.providers.bitbucket.httpx.Client")
def test_post_review_comments_single_line_no_from(mock_client):
    """Single-line comments must NOT include 'from' in the inline anchor.

    The Bitbucket Cloud API spec says 'from' is the start of a multi-line range;
    for single-line comments it should be omitted (null).  Setting 'from' equal to
    'to' on an added/context line can cause the API to reject the comment (returning
    4xx) or silently downgrade it to an activity-feed comment instead of an inline
    diff comment.
    """
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = {"id": 1}
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    p.post_review_comments(
        "owner",
        "repo",
        1,
        [InlineComment(path="foo.py", line=42, body="Issue here")],
    )
    payload = mock_client.return_value.__enter__.return_value.post.call_args[1]["json"]
    assert "from" not in payload["inline"], (
        "Single-line comments must omit 'from' so Bitbucket Cloud places them inline "
        "in the diff view rather than rejecting or demoting them to PR-level comments"
    )
    assert payload["inline"]["to"] == 42


@patch("code_review.providers.bitbucket.httpx.Client")
def test_post_review_comments_multiline_includes_from(mock_client):
    """Multi-line range comments (end_line != line) MUST include 'from'."""
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = {"id": 1}
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    p.post_review_comments(
        "owner",
        "repo",
        1,
        [InlineComment(path="foo.py", line=10, end_line=15, body="Range comment")],
    )
    payload = mock_client.return_value.__enter__.return_value.post.call_args[1]["json"]
    assert payload["inline"]["from"] == 10
    assert payload["inline"]["to"] == 15


@patch("code_review.providers.bitbucket.httpx.Client")
def test_get_existing_review_comments(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "values": [
            {"id": 1, "content": {"raw": "[Critical] Bug"}, "inline": {"path": "foo.py", "to": 10}},
            {"id": 2, "content": {"raw": "[Info] Nit"}, "inline": {"path": "bar.py", "to": 5}},
        ]
    }
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    comments = p.get_existing_review_comments("owner", "repo", 1)
    assert len(comments) == 2
    assert comments[0].id == "1"
    assert comments[0].path == "foo.py"
    assert comments[0].line == 10


@patch("code_review.providers.bitbucket.httpx.Client")
def test_post_pr_summary_comment(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = {"id": 1}
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    p.post_pr_summary_comment("owner", "repo", 1, "Summary body")
    call_args = mock_client.return_value.__enter__.return_value.post.call_args
    assert call_args[1]["json"]["content"]["raw"] == "Summary body"
    assert "inline" not in call_args[1]["json"] or call_args[1]["json"].get("inline") is None


@patch("code_review.providers.bitbucket.httpx.Client")
def test_get_pr_info(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "title": "Fix bug",
        "labels": [{"name": "skip-review"}, {"name": "bug"}],
    }
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    info = p.get_pr_info("owner", "repo", 1)
    assert info is not None
    assert info.title == "Fix bug"
    assert "skip-review" in info.labels
