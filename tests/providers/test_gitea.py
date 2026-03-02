"""Tests for Gitea provider (mocked HTTP)."""

import base64
from unittest.mock import MagicMock, patch

import pytest

from code_review.providers import GiteaProvider, get_provider


def test_get_provider():
    p = get_provider("gitea", "https://gitea.example.com", "token")
    assert isinstance(p, GiteaProvider)


def test_get_provider_unknown():
    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("unknown", "https://x.com", "t")


@patch("code_review.providers.gitea.httpx.Client")
def test_get_pr_diff(mock_client):
    mock_resp = MagicMock()
    mock_resp.text = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"
    mock_resp.headers = {}
    mock_client.return_value.__enter__.return_value.request.return_value = mock_resp

    p = GiteaProvider("https://gitea.example.com", "tok")
    diff = p.get_pr_diff("owner", "repo", 1)
    assert "diff --git" in diff


@patch("code_review.providers.gitea.httpx.Client")
def test_get_file_content(mock_client):
    content_b64 = base64.b64encode(b"print('hello')").decode()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"content": content_b64}
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.request.return_value = mock_resp

    p = GiteaProvider("https://gitea.example.com", "tok")
    content = p.get_file_content("owner", "repo", "main", "foo.py")
    assert content == "print('hello')"


@patch("code_review.providers.gitea.httpx.Client")
def test_get_pr_files(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"filename": "foo.py", "status": "modified", "additions": 5, "deletions": 2},
        {"filename": "bar.go", "status": "added", "additions": 10, "deletions": 0},
    ]
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.request.return_value = mock_resp

    p = GiteaProvider("https://gitea.example.com", "tok")
    files = p.get_pr_files("owner", "repo", 1)
    assert len(files) == 2
    assert files[0].path == "foo.py"
    assert files[0].status == "modified"
    assert files[1].path == "bar.go"
    assert files[1].status == "added"


@patch("code_review.providers.gitea.httpx.Client")
def test_post_review_comments(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.content = b""
    mock_client.return_value.__enter__.return_value.request.return_value = mock_post

    from code_review.providers.base import InlineComment

    p = GiteaProvider("https://gitea.example.com", "tok")
    p.post_review_comments(
        "owner",
        "repo",
        1,
        [InlineComment(path="foo.py", line=10, body="[Critical] Bug here")],
        head_sha="abc123",
    )
    call_args = mock_client.return_value.__enter__.return_value.request.call_args
    assert call_args[0][1].endswith("/reviews")  # url is second positional arg
    payload = call_args[1]["json"]
    assert payload["comments"] == [
        {"path": "foo.py", "body": "[Critical] Bug here", "line": 10}
    ]
    assert payload["commit_id"] == "abc123"


@patch("code_review.providers.gitea.httpx.Client")
def test_get_existing_review_comments(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"id": 1, "path": "foo.py", "line": 10, "body": "[Critical] Bug", "resolved": False},
        {"id": 2, "path": "bar.py", "line": 5, "body": "[Info] Nit", "resolved": True},
    ]
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.request.return_value = mock_resp

    p = GiteaProvider("https://gitea.example.com", "tok")
    comments = p.get_existing_review_comments("owner", "repo", 1)
    assert len(comments) == 2
    assert comments[0].id == "1" and comments[0].resolved is False
    assert comments[1].id == "2" and comments[1].resolved is True


@patch("code_review.providers.gitea.httpx.Client")
def test_get_pr_diff_for_file(mock_client):
    full_diff = (
        "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"
        "@@ -1,2 +1,3 @@\n x\n+y\n z"
    )
    mock_resp = MagicMock()
    mock_resp.text = full_diff
    mock_resp.headers = {}
    mock_client.return_value.__enter__.return_value.request.return_value = mock_resp

    p = GiteaProvider("https://gitea.example.com", "tok")
    diff = p.get_pr_diff_for_file("owner", "repo", 1, "foo.py")
    assert "foo.py" in diff
    assert "+y" in diff


@patch("code_review.providers.gitea.httpx.Client")
def test_get_pr_diff_for_file_multi_hunk_single_header(mock_client):
    """Multi-hunk file must emit ---/+++ once, then multiple @@ sections."""
    full_diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n+++ b/foo.py\n"
        "@@ -1,2 +1,3 @@\n x\n+y\n z\n"
        "@@ -5,1 +6,2 @@\n a\n+b\n"
    )
    mock_resp = MagicMock()
    mock_resp.text = full_diff
    mock_resp.headers = {}
    mock_client.return_value.__enter__.return_value.request.return_value = mock_resp

    p = GiteaProvider("https://gitea.example.com", "tok")
    diff = p.get_pr_diff_for_file("owner", "repo", 1, "foo.py")
    assert diff.count("--- a/foo.py") == 1
    assert diff.count("+++ b/foo.py") == 1
    assert diff.count("@@ ") == 2


@patch("code_review.providers.gitea.httpx.Client")
def test_get_file_lines(mock_client):
    content_b64 = base64.b64encode(b"line1\nline2\nline3\nline4").decode()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"content": content_b64}
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.request.return_value = mock_resp

    p = GiteaProvider("https://gitea.example.com", "tok")
    lines = p.get_file_lines("owner", "repo", "main", "foo.py", 2, 3)
    assert lines == "line2\nline3"


@patch("code_review.providers.gitea.httpx.Client")
def test_post_pr_summary_comment(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.content = b""
    mock_client.return_value.__enter__.return_value.request.return_value = mock_post

    p = GiteaProvider("https://gitea.example.com", "tok")
    p.post_pr_summary_comment("owner", "repo", 1, "Summary: 2 Critical, 1 Suggestion")
    call_args = mock_client.return_value.__enter__.return_value.request.call_args
    assert "/issues/1/comments" in call_args[0][1]  # url is second positional arg
    assert call_args[1]["json"]["body"] == "Summary: 2 Critical, 1 Suggestion"


def test_capabilities():
    p = GiteaProvider("https://gitea.example.com", "tok")
    caps = p.capabilities()
    assert caps.resolvable_comments is False
    assert caps.supports_suggestions is False

