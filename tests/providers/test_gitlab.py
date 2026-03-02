"""Tests for GitLab provider (mocked HTTP)."""

from unittest.mock import MagicMock, patch

from code_review.providers import get_provider
from code_review.providers.gitlab import GitLabProvider
from code_review.providers.base import InlineComment


def test_get_provider_gitlab():
    p = get_provider("gitlab", "https://gitlab.example.com/api/v4", "token")
    assert isinstance(p, GitLabProvider)


@patch("code_review.providers.gitlab.httpx.Client")
def test_get_pr_diff(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"new_path": "foo.py", "old_path": "foo.py", "diff": "--- a/foo.py\n+++ b/foo.py\n@@ -1,3 +1,4 @@\n line\n"}
    ]
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    diff = p.get_pr_diff("owner", "repo", 1)
    assert "foo.py" in diff
    assert "--- a/" in diff or "---" in diff


@patch("code_review.providers.gitlab.httpx.Client")
def test_get_file_content(mock_client):
    mock_resp = MagicMock()
    mock_resp.content = b"print('hello')"
    mock_resp.headers = {}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    content = p.get_file_content("owner", "repo", "main", "foo.py")
    assert content == "print('hello')"


@patch("code_review.providers.gitlab.httpx.Client")
def test_get_pr_files(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"new_path": "foo.py", "old_path": "foo.py", "new_file": False, "deleted_file": False, "diff": ""},
        {"new_path": "bar.go", "old_path": "bar.go", "new_file": True, "deleted_file": False, "diff": ""},
    ]
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    files = p.get_pr_files("owner", "repo", 1)
    assert len(files) == 2
    assert files[0].path == "foo.py"
    assert files[0].status == "modified"
    assert files[1].path == "bar.go"
    assert files[1].status == "added"


@patch("code_review.providers.gitlab.httpx.Client")
def test_post_review_comments(mock_client):
    # First call: get MR for diff_refs
    mr_resp = MagicMock()
    mr_resp.json.return_value = {
        "diff_refs": {"base_sha": "base123", "head_sha": "head123", "start_sha": "start123"},
    }
    mr_resp.headers = {"content-type": "application/json"}
    # Second: post discussion
    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    post_resp.json.return_value = {"id": "disc1"}
    get_calls = [mr_resp, mr_resp]  # two MR fetches if we post two comments separately, or one then post
    mock_client.return_value.__enter__.return_value.get.side_effect = get_calls
    mock_client.return_value.__enter__.return_value.post.return_value = post_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    p.post_review_comments(
        "owner", "repo", 1,
        [InlineComment(path="foo.py", line=10, body="[Critical] Bug here")],
        head_sha="head123",
    )
    post_call = mock_client.return_value.__enter__.return_value.post.call_args
    payload = post_call[1]["json"]
    assert "body" in payload
    assert "[Critical] Bug here" in payload["body"]
    assert payload.get("position", {}).get("new_path") == "foo.py"
    assert payload["position"].get("new_line") == 10


@patch("code_review.providers.gitlab.httpx.Client")
def test_post_review_comments_with_suggested_patch(mock_client):
    # First call: get MR for diff_refs
    mr_resp = MagicMock()
    mr_resp.json.return_value = {
        "diff_refs": {"base_sha": "base123", "head_sha": "head123", "start_sha": "start123"},
    }
    mr_resp.headers = {"content-type": "application/json"}
    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    post_resp.json.return_value = {"id": "disc1"}
    mock_client.return_value.__enter__.return_value.get.return_value = mr_resp
    mock_client.return_value.__enter__.return_value.post.return_value = post_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    p.post_review_comments(
        "owner",
        "repo",
        1,
        [
            InlineComment(
                path="foo.py",
                line=10,
                body="[Suggestion] Consider refactor.",
                suggested_patch="replacement_code();",
            )
        ],
        head_sha="head123",
    )
    post_call = mock_client.return_value.__enter__.return_value.post.call_args
    payload = post_call[1]["json"]
    body = payload["body"]
    assert "[Suggestion] Consider refactor." in body
    assert "```suggestion" in body
    assert "replacement_code();" in body


@patch("code_review.providers.gitlab.httpx.Client")
def test_get_existing_review_comments(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {
            "id": "disc1",
            "notes": [
                {
                    "id": 1,
                    "type": "DiffNote",
                    "body": "[Critical] Bug",
                    "position": {"new_path": "foo.py", "new_line": 10},
                    "resolved": False,
                }
            ],
        },
        {
            "id": "disc2",
            "notes": [
                {
                    "id": 2,
                    "type": "DiffNote",
                    "body": "[Info] Nit",
                    "position": {"new_path": "bar.py", "new_line": 5},
                    "resolved": True,
                }
            ],
        },
    ]
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    comments = p.get_existing_review_comments("owner", "repo", 1)
    assert len(comments) == 2
    assert comments[0].id == "1" and comments[0].path == "foo.py" and comments[0].resolved is False
    assert comments[1].id == "2" and comments[1].resolved is True


@patch("code_review.providers.gitlab.httpx.Client")
def test_post_pr_summary_comment(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = {"id": 1}
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    p.post_pr_summary_comment("owner", "repo", 1, "Summary body")
    call_args = mock_client.return_value.__enter__.return_value.post.call_args
    assert "discussions" in call_args[0][0] or "notes" in call_args[0][0]
    assert call_args[1]["json"]["body"] == "Summary body"


@patch("code_review.providers.gitlab.httpx.Client")
def test_get_pr_info(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "title": "Fix bug",
        "labels": [{"name": "skip-review"}, {"name": "bug"}],
    }
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    info = p.get_pr_info("owner", "repo", 1)
    assert info is not None
    assert info.title == "Fix bug"
    assert "skip-review" in info.labels
