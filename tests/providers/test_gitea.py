"""Tests for Gitea provider (mocked HTTP)."""

import base64
from unittest.mock import MagicMock, patch

import httpx
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
def test_get_incremental_pr_diff_uses_compare_endpoint(mock_client):
    mock_resp = MagicMock()
    mock_resp.text = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"
    mock_resp.headers = {}
    mock_client.return_value.__enter__.return_value.request.return_value = mock_resp

    p = GiteaProvider("https://gitea.example.com", "tok")
    diff = p.get_incremental_pr_diff("owner", "repo", 1, "base123", "head456")

    assert "diff --git" in diff
    call = mock_client.return_value.__enter__.return_value.request.call_args
    assert call[0][1].endswith("/compare/base123...head456.diff")


@patch("code_review.providers.gitea.httpx.Client")
def test_get_incremental_pr_diff_falls_back_to_full_pr_diff_on_compare_404(mock_client):
    compare_resp = MagicMock(status_code=404, text="not found")
    compare_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "compare failed",
        request=MagicMock(),
        response=compare_resp,
    )
    full_pr_resp = MagicMock()
    full_pr_resp.text = "diff --git a/full.py b/full.py\n--- a/full.py\n+++ b/full.py"
    full_pr_resp.headers = {}
    full_pr_resp.raise_for_status = MagicMock()
    mock_client.return_value.__enter__.return_value.request.side_effect = [
        compare_resp,
        full_pr_resp,
    ]

    p = GiteaProvider("https://gitea.example.com", "tok")
    diff = p.get_incremental_pr_diff("owner", "repo", 1, "base123", "head456")

    assert "diff --git a/full.py b/full.py" in diff
    calls = mock_client.return_value.__enter__.return_value.request.call_args_list
    assert calls[0][0][1].endswith("/compare/base123...head456.diff")
    assert calls[1][0][1].endswith("/pulls/1.diff")


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
def test_get_incremental_pr_files_uses_compare_endpoint(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "files": [
            {"filename": "foo.py", "status": "modified", "additions": 1, "deletions": 0}
        ]
    }
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.request.return_value = mock_resp

    p = GiteaProvider("https://gitea.example.com", "tok")
    files = p.get_incremental_pr_files("owner", "repo", 1, "base123", "head456")

    assert len(files) == 1
    assert files[0].path == "foo.py"
    call = mock_client.return_value.__enter__.return_value.request.call_args
    assert call[0][1].endswith("/compare/base123...head456")


@patch("code_review.providers.gitea.httpx.Client")
def test_get_incremental_pr_files_fall_back_to_full_pr_files_on_compare_422(mock_client):
    compare_resp = MagicMock(status_code=422, text="invalid range")
    compare_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "compare failed",
        request=MagicMock(),
        response=compare_resp,
    )
    full_pr_resp = MagicMock()
    full_pr_resp.json.return_value = [
        {"filename": "full.py", "status": "modified", "additions": 2, "deletions": 1}
    ]
    full_pr_resp.headers = {"content-type": "application/json"}
    full_pr_resp.raise_for_status = MagicMock()
    mock_client.return_value.__enter__.return_value.request.side_effect = [
        compare_resp,
        full_pr_resp,
    ]

    p = GiteaProvider("https://gitea.example.com", "tok")
    files = p.get_incremental_pr_files("owner", "repo", 1, "base123", "head456")

    assert len(files) == 1
    assert files[0].path == "full.py"
    calls = mock_client.return_value.__enter__.return_value.request.call_args_list
    assert calls[0][0][1].endswith("/compare/base123...head456")
    assert calls[1][0][1].endswith("/pulls/1/files")


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
        [InlineComment(path="foo.py", line=10, body="[High] Bug here")],
        head_sha="abc123",
    )
    call_args = mock_client.return_value.__enter__.return_value.request.call_args
    assert call_args[0][1].endswith("/reviews")  # url is second positional arg
    payload = call_args[1]["json"]
    assert payload["comments"] == [
        {
            "path": "foo.py",
            "body": "[High] Bug here",
            "old_position": 0,
            "new_position": 10,
        }
    ]
    assert payload["commit_id"] == "abc123"


@patch("code_review.providers.gitea.httpx.Client")
def test_post_review_comments_with_diff_hunk(mock_client):
    """When get_pr_diff returns a valid diff, comments include diff_hunk for diff view."""
    from code_review.providers.base import InlineComment

    diff_body = (
        "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,3 @@\n x\n+y\n z\n"
    )
    mock_diff = MagicMock()
    mock_diff.text = diff_body
    mock_diff.headers = {}
    mock_diff.raise_for_status = MagicMock()
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.content = b""
    mock_client.return_value.__enter__.return_value.request.side_effect = [
        mock_diff,
        mock_post,
    ]

    p = GiteaProvider("https://gitea.example.com", "tok")
    p.post_review_comments(
        "owner",
        "repo",
        1,
        [InlineComment(path="foo.py", line=2, body="[Medium] Consider y")],
        head_sha="abc123",
    )
    calls = mock_client.return_value.__enter__.return_value.request.call_args_list
    assert len(calls) >= 2
    payload = calls[1][1]["json"]
    assert len(payload["comments"]) == 1
    assert payload["comments"][0]["path"] == "foo.py"
    assert payload["comments"][0]["new_position"] == 2
    assert "diff_hunk" in payload["comments"][0]
    assert "@@" in payload["comments"][0]["diff_hunk"]
    assert "+y" in payload["comments"][0]["diff_hunk"]


@patch("code_review.providers.gitea.httpx.Client")
def test_post_review_comments_path_normalized(mock_client):
    """Path is normalized (no leading slash)."""
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
        [InlineComment(path="/src/foo.py", line=1, body="Comment")],
        head_sha="sha",
    )
    payload = mock_client.return_value.__enter__.return_value.request.call_args[1]["json"]
    assert payload["comments"][0]["path"] == "src/foo.py"


@patch("code_review.providers.gitea.httpx.Client")
def test_post_review_comments_get_pr_diff_raises(mock_client):
    """When get_pr_diff raises, comments are still posted without diff_hunk."""
    from code_review.providers.base import InlineComment

    mock_client.return_value.__enter__.return_value.request.side_effect = [
        Exception("network error"),
        MagicMock(raise_for_status=MagicMock(), content=b""),
    ]
    p = GiteaProvider("https://gitea.example.com", "tok")
    p.post_review_comments(
        "owner",
        "repo",
        1,
        [InlineComment(path="foo.py", line=1, body="Comment")],
        head_sha="sha",
    )
    payload = mock_client.return_value.__enter__.return_value.request.call_args_list[1][1]["json"]
    assert payload["comments"][0]["path"] == "foo.py"
    assert "diff_hunk" not in payload["comments"][0]


@patch("code_review.providers.gitea.httpx.Client")
def test_post_review_comments_empty_path_fallback(mock_client):
    """When path is empty or only slashes, fallback to c.path."""
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
        [InlineComment(path="", line=1, body="Comment")],
        head_sha="sha",
    )
    payload = mock_client.return_value.__enter__.return_value.request.call_args[1]["json"]
    assert payload["comments"][0]["path"] == ""


@patch("code_review.providers.gitea.httpx.Client")
def test_get_existing_review_comments(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"id": 1, "path": "foo.py", "line": 10, "body": "[High] Bug", "resolved": False},
        {"id": 2, "path": "bar.py", "line": 5, "body": "[Low] Nit", "resolved": True},
    ]
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.request.return_value = mock_resp

    p = GiteaProvider("https://gitea.example.com", "tok")
    comments = p.get_existing_review_comments("owner", "repo", 1)
    assert len(comments) == 2
    assert comments[0].id == "1" and comments[0].resolved is False
    assert comments[1].id == "2" and comments[1].resolved is True


@patch("code_review.providers.gitea.httpx.Client")
def test_submit_review_decision(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.content = b""
    mock_client.return_value.__enter__.return_value.request.return_value = mock_post

    p = GiteaProvider("https://gitea.example.com", "tok")
    p.submit_review_decision(
        "owner",
        "repo",
        1,
        "APPROVE",
        body="Automated threshold decision",
        head_sha="abc123",
    )
    call_args = mock_client.return_value.__enter__.return_value.request.call_args
    assert call_args[0][0] == "POST"
    assert call_args[0][1].endswith("/repos/owner/repo/pulls/1/reviews")
    payload = call_args[1]["json"]
    assert payload["event"] == "APPROVE"
    assert payload["body"] == "Automated threshold decision"
    assert payload["commit_id"] == "abc123"


@pytest.mark.parametrize("status", [404, 405, 501])
@patch("code_review.providers.gitea.httpx.Client")
def test_submit_review_decision_unsupported_http_is_no_op(mock_client, status):
    mock_resp = MagicMock()
    mock_resp.status_code = status
    exc = httpx.HTTPStatusError("nope", request=MagicMock(), response=mock_resp)
    req = mock_client.return_value.__enter__.return_value.request.return_value
    req.raise_for_status.side_effect = exc

    p = GiteaProvider("https://gitea.example.com", "tok")
    p.submit_review_decision("owner", "repo", 1, "APPROVE", body="x", head_sha="sha")


@patch("code_review.providers.gitea.httpx.Client")
def test_submit_review_decision_re_raises_other_http_errors(mock_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    exc = httpx.HTTPStatusError("err", request=MagicMock(), response=mock_resp)
    req = mock_client.return_value.__enter__.return_value.request.return_value
    req.raise_for_status.side_effect = exc

    p = GiteaProvider("https://gitea.example.com", "tok")
    with pytest.raises(httpx.HTTPStatusError):
        p.submit_review_decision("owner", "repo", 1, "APPROVE")


@patch("code_review.providers.gitea.httpx.Client")
def test_get_pr_diff_for_file(mock_client):
    full_diff = (
        "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n" + "@@ -1,2 +1,3 @@\n x\n+y\n z"
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


@patch.object(GiteaProvider, "_patch")
def test_update_pr_description(mock_patch):
    """update_pr_description PATCHes the pull request with body (and optional title)."""
    p = GiteaProvider("https://gitea.example.com", "tok")
    p.update_pr_description("owner", "repo", 42, "**Title**: kafka\n\nThis PR updates 2 files.")
    mock_patch.assert_called_once_with(
        "/repos/owner/repo/pulls/42",
        {"body": "**Title**: kafka\n\nThis PR updates 2 files."},
    )


@patch.object(GiteaProvider, "_patch")
def test_update_pr_description_with_title(mock_patch):
    """update_pr_description can also set the PR title."""
    p = GiteaProvider("https://gitea.example.com", "tok")
    p.update_pr_description("o", "r", 1, "New body.", title="New title")
    mock_patch.assert_called_once_with(
        "/repos/o/r/pulls/1", {"body": "New body.", "title": "New title"}
    )


def test_capabilities():
    p = GiteaProvider("https://gitea.example.com", "tok")
    caps = p.capabilities()
    assert caps.resolvable_comments is False
    assert caps.supports_suggestions is True
    assert caps.supports_review_decisions is True
