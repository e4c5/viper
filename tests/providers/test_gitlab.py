"""Tests for GitLab provider (mocked HTTP)."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from code_review.providers import get_provider
from code_review.providers.base import InlineComment
from code_review.providers.gitlab import GitLabProvider


def test_get_provider_gitlab():
    p = get_provider("gitlab", "https://gitlab.example.com/api/v4", "token")
    assert isinstance(p, GitLabProvider)


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_pr_diff(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {
            "new_path": "foo.py",
            "old_path": "foo.py",
            "diff": "--- a/foo.py\n+++ b/foo.py\n@@ -1,3 +1,4 @@\n line\n",
        }
    ]
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    diff = p.get_pr_diff("owner", "repo", 1)
    assert "foo.py" in diff
    assert "--- a/" in diff or "---" in diff


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_incremental_pr_diff_uses_compare_endpoint(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "diffs": [
            {
                "new_path": "foo.py",
                "old_path": "foo.py",
                "diff": "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n",
            }
        ]
    }
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    diff = p.get_incremental_pr_diff("owner", "repo", 1, "base123", "head456")

    assert "foo.py" in diff
    call = mock_client.return_value.__enter__.return_value.get.call_args
    assert "/repository/compare?from=base123&to=head456&straight=true" in call[0][0]


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_incremental_pr_diff_falls_back_to_full_pr_diff_on_compare_404(mock_client):
    compare_resp = MagicMock(status_code=404, text="not found")
    compare_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "compare failed",
        request=MagicMock(),
        response=compare_resp,
    )
    full_mr_resp = MagicMock()
    full_mr_resp.json.return_value = [
        {
            "new_path": "full.py",
            "old_path": "full.py",
            "diff": "--- a/full.py\n+++ b/full.py\n@@ -1 +1 @@\n-old\n+new\n",
        }
    ]
    full_mr_resp.headers = {"content-type": "application/json"}
    full_mr_resp.raise_for_status = MagicMock()
    mock_client.return_value.__enter__.return_value.get.side_effect = [
        compare_resp,
        full_mr_resp,
    ]

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    diff = p.get_incremental_pr_diff("owner", "repo", 1, "base123", "head456")

    assert "diff --git a/full.py b/full.py" in diff
    calls = mock_client.return_value.__enter__.return_value.get.call_args_list
    assert "/repository/compare?from=base123&to=head456&straight=true" in calls[0][0][0]
    assert "/merge_requests/1/diffs" in calls[1][0][0]


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_incremental_pr_diff_re_raises_non_fallback_compare_error(mock_client):
    compare_resp = MagicMock(status_code=500, text="server error")
    compare_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "compare failed",
        request=MagicMock(),
        response=compare_resp,
    )
    mock_client.return_value.__enter__.return_value.get.return_value = compare_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")

    with pytest.raises(httpx.HTTPStatusError):
        p.get_incremental_pr_diff("owner", "repo", 1, "base123", "head456")


@patch("code_review.providers.gitlab.httpx.Client")
def test_get_file_content(mock_client):
    mock_resp = MagicMock()
    mock_resp.content = b"print('hello')"
    mock_resp.headers = {}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    content = p.get_file_content("owner", "repo", "main", "foo.py")
    assert content == "print('hello')"


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_pr_files(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {
            "new_path": "foo.py",
            "old_path": "foo.py",
            "new_file": False,
            "deleted_file": False,
            "diff": "",
        },
        {
            "new_path": "bar.go",
            "old_path": "bar.go",
            "new_file": True,
            "deleted_file": False,
            "diff": "",
        },
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


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_incremental_pr_files_uses_compare_endpoint(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "diffs": [
            {
                "new_path": "foo.py",
                "old_path": "foo.py",
                "new_file": False,
                "deleted_file": False,
                "diff": "",
            }
        ]
    }
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    files = p.get_incremental_pr_files("owner", "repo", 1, "base123", "head456")

    assert len(files) == 1
    assert files[0].path == "foo.py"
    call = mock_client.return_value.__enter__.return_value.get.call_args
    assert "/repository/compare?from=base123&to=head456&straight=true" in call[0][0]


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_incremental_pr_files_fall_back_to_full_pr_files_on_compare_422(mock_client):
    compare_resp = MagicMock(status_code=422, text="invalid range")
    compare_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "compare failed",
        request=MagicMock(),
        response=compare_resp,
    )
    full_mr_resp = MagicMock()
    full_mr_resp.json.return_value = [
        {
            "new_path": "full.py",
            "old_path": "full.py",
            "new_file": False,
            "deleted_file": False,
            "diff": "",
        }
    ]
    full_mr_resp.headers = {"content-type": "application/json"}
    full_mr_resp.raise_for_status = MagicMock()
    mock_client.return_value.__enter__.return_value.get.side_effect = [
        compare_resp,
        full_mr_resp,
    ]

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    files = p.get_incremental_pr_files("owner", "repo", 1, "base123", "head456")

    assert len(files) == 1
    assert files[0].path == "full.py"
    calls = mock_client.return_value.__enter__.return_value.get.call_args_list
    assert "/repository/compare?from=base123&to=head456&straight=true" in calls[0][0][0]
    assert "/merge_requests/1/diffs" in calls[1][0][0]


@patch("code_review.providers.http_shortcuts.httpx.Client")
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
    # Two MR fetches if we post two comments separately, or one then post.
    get_calls = [mr_resp, mr_resp]
    mock_client.return_value.__enter__.return_value.get.side_effect = get_calls
    mock_client.return_value.__enter__.return_value.post.return_value = post_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    p.post_review_comments(
        "owner",
        "repo",
        1,
        [InlineComment(path="foo.py", line=10, body="[High] Bug here")],
        head_sha="head123",
    )
    post_call = mock_client.return_value.__enter__.return_value.post.call_args
    payload = post_call[1]["json"]
    assert "body" in payload
    assert "[High] Bug here" in payload["body"]
    assert payload.get("position", {}).get("new_path") == "foo.py"
    assert payload["position"].get("new_line") == 10


@patch("code_review.providers.http_shortcuts.httpx.Client")
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
                body="[Medium] Consider refactor.",
                suggested_patch="replacement_code();",
            )
        ],
        head_sha="head123",
    )
    post_call = mock_client.return_value.__enter__.return_value.post.call_args
    payload = post_call[1]["json"]
    body = payload["body"]
    assert "[Medium] Consider refactor." in body
    assert "```suggestion" in body
    assert "replacement_code();" in body


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_existing_review_comments(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {
            "id": "disc1",
            "notes": [
                {
                    "id": 1,
                    "type": "DiffNote",
                    "body": "[High] Bug",
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
                    "body": "[Low] Nit",
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


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_unresolved_review_items_skips_resolved_discussions(mock_client):
    """Quality gate uses discussion-level resolved; one thread row per discussion."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {
            "id": "disc-open",
            "resolved": False,
            "notes": [
                {
                    "id": 1,
                    "type": "DiffNote",
                    "body": "[Low] Minor",
                    "position": {"new_path": "a.py", "new_line": 1},
                },
                {
                    "id": 2,
                    "type": "DiffNote",
                    "body": "[High] Must fix",
                    "position": {"new_path": "a.py", "new_line": 2},
                },
            ],
        },
        {
            "id": "disc-resolved",
            "resolved": True,
            "notes": [
                {
                    "id": 3,
                    "type": "DiffNote",
                    "body": "[High] Gone",
                    "position": {"new_path": "b.py", "new_line": 1},
                }
            ],
        },
    ]
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    items = p.get_unresolved_review_items_for_quality_gate("owner", "repo", 1)
    assert len(items) == 1
    assert items[0].thread_id == "disc-open"
    assert items[0].inferred_severity == "high"
    assert items[0].kind == "discussion_thread"


def _json_get_response(payload):
    m = MagicMock()
    m.headers = {"content-type": "application/json"}
    m.json.return_value = payload
    return m


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_unresolved_review_items_paginates_discussions(mock_client):
    """Quality gate must follow GitLab discussions pagination (page/per_page)."""
    note = {
        "id": 1,
        "type": "DiffNote",
        "body": "[Low] x",
        "position": {"new_path": "f.py", "new_line": 1},
    }
    page1 = [
        {
            "id": str(i),
            "resolved": False,
            "notes": [dict(note, id=i)],
        }
        for i in range(100)
    ]
    page2 = [
        {
            "id": "page-two",
            "resolved": False,
            "notes": [
                {
                    "id": 200,
                    "type": "DiffNote",
                    "body": "[High] second page",
                    "position": {"new_path": "g.py", "new_line": 2},
                }
            ],
        }
    ]
    mock_client.return_value.__enter__.return_value.get.side_effect = [
        _json_get_response(page1),
        _json_get_response(page2),
    ]

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    items = p.get_unresolved_review_items_for_quality_gate("owner", "repo", 1)

    assert len(items) == 101
    assert any(i.thread_id == "page-two" and i.inferred_severity == "high" for i in items)
    urls = [c[0][0] for c in mock_client.return_value.__enter__.return_value.get.call_args_list]
    assert any("page=1" in u and "per_page=100" in u for u in urls)
    assert any("page=2" in u for u in urls)


@patch("code_review.providers.http_shortcuts.httpx.Client")
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


@patch("code_review.providers.http_shortcuts.httpx.Client")
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


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_submit_review_decision_approve(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.content = b""
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    p.submit_review_decision("owner", "repo", 7, "APPROVE", body="ok", head_sha="deadbeef")
    call_args = mock_client.return_value.__enter__.return_value.post.call_args
    assert "/merge_requests/7/approve" in call_args[0][0]
    assert call_args[1]["json"] == {"sha": "deadbeef"}


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_submit_review_decision_request_changes_note(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = {"id": 1}
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    p.submit_review_decision("owner", "repo", 7, "REQUEST_CHANGES", body="fix it")
    call_args = mock_client.return_value.__enter__.return_value.post.call_args
    assert "/merge_requests/7/notes" in call_args[0][0]
    assert "/submit_review requested_changes" in call_args[1]["json"]["body"]
    assert "fix it" in call_args[1]["json"]["body"]


def test_gitlab_capabilities_support_review_decisions():
    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    assert p.capabilities().supports_review_decisions is True


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_submit_review_decision_request_changes_unapproves_first(mock_client):
    """REQUEST_CHANGES must DELETE /approve before posting the note (re-run scenario).

    When the agent re-runs on an updated PR and the bot had previously approved,
    submitting REQUEST_CHANGES should first remove that approval so the MR is not
    left in the contradictory "approved + request-changes" state.
    """
    mock_delete_resp = MagicMock()
    mock_delete_resp.raise_for_status = MagicMock()
    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.json.return_value = {"id": 1}
    http = mock_client.return_value.__enter__.return_value
    http.delete.return_value = mock_delete_resp
    http.post.return_value = mock_post_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    p.submit_review_decision("owner", "repo", 7, "REQUEST_CHANGES", body="fix it")

    # DELETE /approve must have been called
    assert http.delete.call_count == 1
    delete_url = http.delete.call_args[0][0]
    assert "/merge_requests/7/approve" in delete_url
    # POST /notes with the quick-action body must follow
    assert http.post.call_count == 1
    assert "/merge_requests/7/notes" in http.post.call_args[0][0]


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_submit_review_decision_request_changes_ignores_404_unapprove(mock_client):
    """A 404 from DELETE /approve (bot had not approved) must be silently ignored."""
    import httpx as real_httpx

    mock_delete_resp = MagicMock()
    mock_delete_resp.status_code = 404
    mock_404 = real_httpx.HTTPStatusError(
        "not found", request=MagicMock(), response=mock_delete_resp
    )
    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.json.return_value = {"id": 1}
    http = mock_client.return_value.__enter__.return_value
    http.delete.return_value = MagicMock(raise_for_status=MagicMock(side_effect=mock_404))
    http.post.return_value = mock_post_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    # Must not raise even though DELETE returned 404
    p.submit_review_decision("owner", "repo", 7, "REQUEST_CHANGES", body="fix it")
    assert http.post.call_count == 1
    assert "/merge_requests/7/notes" in http.post.call_args[0][0]


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_submit_review_decision_approve_does_not_delete(mock_client):
    """APPROVE must NOT call DELETE /approve (no prior state to clean up on approve path)."""
    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.content = b""
    http = mock_client.return_value.__enter__.return_value
    http.post.return_value = mock_post_resp

    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    p.submit_review_decision("owner", "repo", 7, "APPROVE", body="lgtm", head_sha="abc")

    assert http.delete.call_count == 0
    assert http.post.call_count == 1
    assert "/merge_requests/7/approve" in http.post.call_args[0][0]


@patch("code_review.providers.gitlab.http_get_json_or_text")
def test_get_bot_blocking_state_requested_changes_last_wins(mock_get):
    mock_get.side_effect = [
        {"id": 10},
        [
            {"id": 1, "user": {"id": 10}, "state": "approved"},
            {"id": 2, "user": {"id": 10}, "state": "requested_changes"},
        ],
    ]
    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    assert p.get_bot_blocking_state("owner", "repo", 3) == "BLOCKING"


@patch("code_review.providers.gitlab.http_get_json_or_text")
def test_get_bot_blocking_state_not_blocking_when_other_users_only(mock_get):
    mock_get.side_effect = [
        {"id": 10},
        [{"id": 1, "user": {"id": 99}, "state": "requested_changes"}],
    ]
    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    assert p.get_bot_blocking_state("owner", "repo", 3) == "NOT_BLOCKING"


@patch("code_review.providers.gitlab.http_get_json_or_text")
def test_get_bot_blocking_state_unknown_on_reviews_404(mock_get):
    import httpx as real_httpx

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    err = real_httpx.HTTPStatusError("nope", request=MagicMock(), response=mock_resp)
    mock_get.side_effect = [{"id": 10}, err]
    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    assert p.get_bot_blocking_state("owner", "repo", 3) == "UNKNOWN"


@patch("code_review.providers.gitlab.http_get_json_or_text")
def test_get_bot_blocking_state_paginates_reviews(mock_get):
    """Bot review on page 2 must not be ignored (first page only is stale)."""
    page1 = [{"id": i, "user": {"id": 99}, "state": "approved"} for i in range(100)]
    page2 = [
        {"id": 150, "user": {"id": 10}, "state": "approved"},
        {"id": 200, "user": {"id": 10}, "state": "requested_changes"},
    ]
    mock_get.side_effect = [{"id": 10}, page1, page2]
    p = GitLabProvider("https://gitlab.example.com/api/v4", "tok")
    assert p.get_bot_blocking_state("owner", "repo", 3) == "BLOCKING"
