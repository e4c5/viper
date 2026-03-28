"""Tests for Bitbucket provider (mocked HTTP)."""

from unittest.mock import MagicMock, patch

from code_review.providers import get_provider
from code_review.providers.base import InlineComment
from code_review.providers.bitbucket import BitbucketProvider
from code_review.providers.review_decision_common import effective_review_body
from code_review.reply_dismissal_state import REPLY_DISMISSAL_ACCEPTED_REPLY_TEXT


def test_get_provider_bitbucket():
    p = get_provider("bitbucket", "https://api.bitbucket.org/2.0", "token")
    assert isinstance(p, BitbucketProvider)


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_pr_diff(mock_client):
    mock_resp = MagicMock()
    mock_resp.text = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"
    mock_resp.headers = {}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    diff = p.get_pr_diff("owner", "repo", 1)
    assert "diff --git" in diff


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_incremental_pr_diff_uses_compare_endpoint(mock_client):
    mock_resp = MagicMock()
    mock_resp.text = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"
    mock_resp.headers = {}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    diff = p.get_incremental_pr_diff("owner", "repo", 1, "base123", "head456")

    assert "diff --git" in diff
    call = mock_client.return_value.__enter__.return_value.get.call_args
    assert "/diff/base123..head456" in call[0][0]


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_file_content(mock_client):
    mock_resp = MagicMock()
    mock_resp.text = "print('hello')"
    mock_resp.headers = {}
    mock_resp.content = b"print('hello')"
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    content = p.get_file_content("owner", "repo", "main", "foo.py")
    assert content == "print('hello')"


@patch("code_review.providers.http_shortcuts.httpx.Client")
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


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_incremental_pr_files_uses_diffstat_compare_endpoint(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "values": [{"new": {"path": "foo.py"}, "old": {"path": "foo.py"}, "status": "modified"}]
    }
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    files = p.get_incremental_pr_files("owner", "repo", 1, "base123", "head456")

    assert len(files) == 1
    assert files[0].path == "foo.py"
    call = mock_client.return_value.__enter__.return_value.get.call_args
    assert "/diffstat/base123..head456" in call[0][0]


@patch("code_review.providers.http_shortcuts.httpx.Client")
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
        [InlineComment(path="foo.py", line=10, body="[High] Bug here")],
        head_sha="abc123",
    )
    call_args = mock_client.return_value.__enter__.return_value.post.call_args
    assert "comments" in call_args[0][0]
    payload = call_args[1]["json"]
    assert payload["content"]["raw"] == "[High] Bug here"
    assert payload["inline"]["path"] == "foo.py"
    assert payload["inline"]["to"] == 10


@patch("code_review.providers.http_shortcuts.httpx.Client")
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


@patch("code_review.providers.http_shortcuts.httpx.Client")
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


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_unresolved_review_items_open_tasks_only(mock_client):
    """Bitbucket Cloud quality gate merges unresolved inline comments with open PR tasks."""

    def _get_side_effect(url: str, **kwargs):
        mock_r = MagicMock()
        mock_r.headers = {"content-type": "application/json"}
        mock_r.raise_for_status = MagicMock()
        u = str(url)
        if "/pullrequests/5/comments" in u:
            mock_r.json.return_value = {"values": [], "next": None}
        elif "/pullrequests/5/tasks" in u:
            mock_r.json.return_value = {
                "values": [
                    {"id": 10, "state": "OPEN", "content": {"raw": "[High] Do this"}},
                    {"id": 11, "state": "RESOLVED", "content": {"raw": "[High] Done"}},
                ],
                "next": None,
            }
        else:
            mock_r.json.return_value = {}
        return mock_r

    mock_client.return_value.__enter__.return_value.get.side_effect = _get_side_effect

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    items = p.get_unresolved_review_items_for_quality_gate("ws", "slug", 5)
    assert len(items) == 1
    assert items[0].kind == "task"
    assert items[0].inferred_severity == "high"


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_unresolved_review_items_includes_inline_comments_and_tasks(mock_client):
    def _get_side_effect(url: str, **kwargs):
        mock_r = MagicMock()
        mock_r.headers = {"content-type": "application/json"}
        mock_r.raise_for_status = MagicMock()
        u = str(url)
        if "/pullrequests/3/comments" in u:
            mock_r.json.return_value = {
                "values": [
                    {
                        "id": 100,
                        "content": {"raw": "[Medium] fix"},
                        "inline": {"path": "a.py", "to": 2},
                    }
                ],
                "next": None,
            }
        elif "/pullrequests/3/tasks" in u:
            mock_r.json.return_value = {
                "values": [{"id": 20, "state": "OPEN", "content": {"raw": "[High] task"}}],
                "next": None,
            }
        else:
            mock_r.json.return_value = {}
        return mock_r

    mock_client.return_value.__enter__.return_value.get.side_effect = _get_side_effect
    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    items = p.get_unresolved_review_items_for_quality_gate("ws", "slug", 3)
    kinds = {i.kind for i in items}
    assert "inline_comment" in kinds
    assert "task" in kinds
    assert any(i.stable_id == "comment:100" for i in items)
    assert any(i.stable_id.startswith("bbcloud:task:") for i in items)


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_unresolved_review_items_skips_pr_level_and_reply_comments(mock_client):
    def _get_side_effect(url: str, **kwargs):
        mock_r = MagicMock()
        mock_r.headers = {"content-type": "application/json"}
        mock_r.raise_for_status = MagicMock()
        u = str(url)
        if "/pullrequests/9/comments" in u:
            mock_r.json.return_value = {
                "values": [
                    {
                        "id": 100,
                        "content": {"raw": "[Medium] inline root"},
                        "inline": {"path": "a.py", "to": 2},
                    },
                    {
                        "id": 101,
                        "content": {"raw": "[High] reply"},
                        "inline": {"path": "a.py", "to": 2},
                        "parent": {"id": 100},
                    },
                    {
                        "id": 102,
                        "content": {"raw": "[Low] PR level"},
                    },
                ],
                "next": None,
            }
        elif "/pullrequests/9/tasks" in u:
            mock_r.json.return_value = {"values": [], "next": None}
        else:
            mock_r.json.return_value = {}
        return mock_r

    mock_client.return_value.__enter__.return_value.get.side_effect = _get_side_effect
    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    items = p.get_unresolved_review_items_for_quality_gate("ws", "slug", 9)

    assert [i.stable_id for i in items] == ["comment:100"]


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_unresolved_review_items_skips_outdated_inline_comments(mock_client):
    def _get_side_effect(url: str, **kwargs):
        mock_r = MagicMock()
        mock_r.headers = {"content-type": "application/json"}
        mock_r.raise_for_status = MagicMock()
        u = str(url)
        if "/pullrequests/9/comments" in u:
            mock_r.json.return_value = {
                "values": [
                    {
                        "id": 100,
                        "content": {"raw": "[High] already applied"},
                        "inline": {"path": "a.py", "to": 2, "outdated": True},
                    },
                    {
                        "id": 101,
                        "content": {"raw": "[Low] still active"},
                        "inline": {"path": "a.py", "to": 5},
                    },
                ],
                "next": None,
            }
        elif "/pullrequests/9/tasks" in u:
            mock_r.json.return_value = {"values": [], "next": None}
        else:
            mock_r.json.return_value = {}
        return mock_r

    mock_client.return_value.__enter__.return_value.get.side_effect = _get_side_effect
    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    items = p.get_unresolved_review_items_for_quality_gate("ws", "slug", 9)

    assert [i.stable_id for i in items] == ["comment:101"]


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_unresolved_review_items_skips_threads_with_latest_accepted_bot_reply(mock_client):
    def _get_side_effect(url: str, **kwargs):
        mock_r = MagicMock()
        mock_r.headers = {"content-type": "application/json"}
        mock_r.raise_for_status = MagicMock()
        u = str(url)
        if u.endswith("/user"):
            mock_r.json.return_value = {"username": "viper"}
        elif "/pullrequests/9/comments" in u:
            mock_r.json.return_value = {
                "values": [
                    {
                        "id": 100,
                        "content": {"raw": "[Medium] inline root"},
                        "inline": {"path": "a.py", "to": 2},
                        "user": {"nickname": "viper"},
                        "created_on": "2025-01-01T10:00:00Z",
                    },
                    {
                        "id": 101,
                        "content": {"raw": "I fixed it"},
                        "inline": {"path": "a.py", "to": 2},
                        "parent": {"id": 100},
                        "user": {"nickname": "dev"},
                        "created_on": "2025-01-01T11:00:00Z",
                    },
                    {
                        "id": 102,
                        "content": {"raw": REPLY_DISMISSAL_ACCEPTED_REPLY_TEXT},
                        "inline": {"path": "a.py", "to": 2},
                        "parent": {"id": 101},
                        "user": {"nickname": "viper"},
                        "created_on": "2025-01-01T12:00:00Z",
                    },
                ],
                "next": None,
            }
        elif "/pullrequests/9/tasks" in u:
            mock_r.json.return_value = {"values": [], "next": None}
        else:
            mock_r.json.return_value = {}
        return mock_r

    mock_client.return_value.__enter__.return_value.get.side_effect = _get_side_effect
    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")

    items = p.get_unresolved_review_items_for_quality_gate("ws", "slug", 9)

    assert items == []


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_unresolved_review_items_skips_dismissed_thread_when_comment_order_is_reversed(
    mock_client,
):
    def _get_side_effect(url: str, **kwargs):
        mock_r = MagicMock()
        mock_r.headers = {"content-type": "application/json"}
        mock_r.raise_for_status = MagicMock()
        u = str(url)
        if u.endswith("/user"):
            mock_r.json.return_value = {"username": "viper"}
        elif "/pullrequests/9/comments" in u:
            mock_r.json.return_value = {
                "values": [
                    {
                        "id": 102,
                        "content": {"raw": REPLY_DISMISSAL_ACCEPTED_REPLY_TEXT},
                        "inline": {"path": "a.py", "to": 2},
                        "parent": {"id": 101},
                        "user": {"nickname": "viper"},
                        "created_on": "2025-01-01T12:00:00Z",
                    },
                    {
                        "id": 101,
                        "content": {"raw": "I fixed it"},
                        "inline": {"path": "a.py", "to": 2},
                        "parent": {"id": 100},
                        "user": {"nickname": "dev"},
                        "created_on": "2025-01-01T11:00:00Z",
                    },
                    {
                        "id": 100,
                        "content": {"raw": "[Medium] inline root"},
                        "inline": {"path": "a.py", "to": 2},
                        "user": {"nickname": "viper"},
                        "created_on": "2025-01-01T10:00:00Z",
                    },
                ],
                "next": None,
            }
        elif "/pullrequests/9/tasks" in u:
            mock_r.json.return_value = {"values": [], "next": None}
        else:
            mock_r.json.return_value = {}
        return mock_r

    mock_client.return_value.__enter__.return_value.get.side_effect = _get_side_effect
    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")

    items = p.get_unresolved_review_items_for_quality_gate("ws", "slug", 9)

    assert items == []


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_existing_review_comments(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "values": [
            {"id": 1, "content": {"raw": "[High] Bug"}, "inline": {"path": "foo.py", "to": 10}},
            {
                "id": 2,
                "content": {"raw": "[Low] Nit"},
                "inline": {"path": "bar.py", "to": 5},
                "parent": {"id": 1},
            },
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
    assert comments[0].outdated is False
    assert comments[0].parent_id is None
    assert comments[1].parent_id == "1"


@patch("code_review.providers.http_shortcuts.httpx.Client")
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


@patch("code_review.providers.http_shortcuts.httpx.Client")
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


def test_capabilities():
    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    caps = p.capabilities()
    assert caps.supports_suggestions is True
    assert caps.supports_review_decisions is True
    assert caps.supports_review_thread_dismissal_context is True
    assert caps.supports_review_thread_reply is True


def test_bbcloud_build_dismissal_context_thread():
    raw = [
        {
            "id": 1,
            "content": {"raw": "Bot [High] issue"},
            "user": {"nickname": "bot"},
            "created_on": "2025-01-01T10:00:00Z",
        },
        {
            "id": 2,
            "parent": {"id": 1},
            "content": {"raw": "Human fixed"},
            "user": {"nickname": "dev"},
            "created_on": "2025-01-01T11:00:00Z",
        },
    ]
    ctx = BitbucketProvider._bbcloud_build_dismissal_context(raw, "2")
    assert ctx is not None
    assert ctx.gate_exclusion_stable_id == "comment:1"
    assert len(ctx.entries) == 2
    assert ctx.entries[0].comment_id == "1"


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_get_review_thread_dismissal_context_paginates_comments(mock_client):
    mock_r = MagicMock()
    mock_r.headers = {"content-type": "application/json"}
    mock_r.raise_for_status = MagicMock()
    mock_r.json.return_value = {
        "values": [
            {
                "id": 5,
                "content": {"raw": "root"},
                "user": {"nickname": "a"},
                "created_on": "2025-01-01T10:00:00Z",
            },
            {
                "id": 6,
                "parent": {"id": 5},
                "content": {"raw": "reply"},
                "user": {"nickname": "b"},
                "created_on": "2025-01-01T11:00:00Z",
            },
        ],
        "next": None,
    }
    mock_client.return_value.__enter__.return_value.get.return_value = mock_r
    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    ctx = p.get_review_thread_dismissal_context("ws", "slug", 1, "6")
    assert ctx is not None
    assert ctx.gate_exclusion_stable_id == "comment:5"


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_post_review_thread_reply_bitbucket_cloud(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = {"id": 9}
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post
    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    p.post_review_thread_reply("ws", "slug", 2, "42", "Still an issue")
    call = mock_client.return_value.__enter__.return_value.post.call_args
    assert "/pullrequests/2/comments" in call[0][0]
    assert call[1]["json"]["parent"]["id"] == 42
    assert call[1]["json"]["content"]["raw"] == "Still an issue"


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_submit_review_decision_approve(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.content = b""
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    p.submit_review_decision("ws", "repo", 9, "APPROVE", body="x", head_sha="sha")
    posts = mock_client.return_value.__enter__.return_value.post
    assert posts.call_count == 2
    urls = [posts.call_args_list[i][0][0] for i in range(2)]
    assert any(u.endswith("/pullrequests/9/approve") for u in urls)
    comment_call = next(c for c in posts.call_args_list if "/pullrequests/9/comments" in c[0][0])
    assert comment_call[1]["json"]["content"]["raw"] == effective_review_body("x")


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_submit_review_decision_request_changes(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.content = b""
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    p.submit_review_decision("ws", "repo", 9, "REQUEST_CHANGES", body="please fix")
    posts = mock_client.return_value.__enter__.return_value.post
    assert posts.call_count == 2
    urls = [posts.call_args_list[i][0][0] for i in range(2)]
    assert any(u.endswith("/pullrequests/9/request-changes") for u in urls)
    comment_call = next(c for c in posts.call_args_list if "/pullrequests/9/comments" in c[0][0])
    assert comment_call[1]["json"]["content"]["raw"] == effective_review_body("please fix")


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_submit_review_decision_approve_clears_request_changes_first(mock_client):
    """APPROVE must DELETE /request-changes before POSTing /approve (re-run scenario).

    When the agent re-runs on an updated PR that now passes quality gate and the bot had
    previously requested changes, approving should first remove the prior state.
    """
    mock_delete_resp = MagicMock()
    mock_delete_resp.raise_for_status = MagicMock()
    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.content = b""
    http = mock_client.return_value.__enter__.return_value
    http.delete.return_value = mock_delete_resp
    http.post.return_value = mock_post_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    p.submit_review_decision("ws", "repo", 9, "APPROVE", body="lgtm")

    assert http.delete.call_count == 1
    assert http.delete.call_args[0][0].endswith("/pullrequests/9/request-changes")
    urls = [http.post.call_args_list[i][0][0] for i in range(http.post.call_count)]
    assert any(u.endswith("/pullrequests/9/approve") for u in urls)


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_submit_review_decision_request_changes_clears_approve_first(mock_client):
    """REQUEST_CHANGES must DELETE /approve before POSTing /request-changes (re-run scenario)."""
    mock_delete_resp = MagicMock()
    mock_delete_resp.raise_for_status = MagicMock()
    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.content = b""
    http = mock_client.return_value.__enter__.return_value
    http.delete.return_value = mock_delete_resp
    http.post.return_value = mock_post_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    p.submit_review_decision("ws", "repo", 9, "REQUEST_CHANGES", body="please fix")

    assert http.delete.call_count == 1
    assert http.delete.call_args[0][0].endswith("/pullrequests/9/approve")
    urls = [http.post.call_args_list[i][0][0] for i in range(http.post.call_count)]
    assert any(u.endswith("/pullrequests/9/request-changes") for u in urls)


@patch("code_review.providers.http_shortcuts.httpx.Client")
def test_submit_review_decision_approve_ignores_404_on_clear(mock_client):
    """A 404 from DELETE /request-changes (nothing to clear) must be silently ignored."""
    import httpx as real_httpx

    mock_delete_resp = MagicMock()
    mock_delete_resp.status_code = 404
    mock_404 = real_httpx.HTTPStatusError(
        "not found", request=MagicMock(), response=mock_delete_resp
    )
    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    mock_post_resp.content = b""
    http = mock_client.return_value.__enter__.return_value
    http.delete.return_value = MagicMock(raise_for_status=MagicMock(side_effect=mock_404))
    http.post.return_value = mock_post_resp

    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    p.submit_review_decision("ws", "repo", 9, "APPROVE", body="lgtm")

    urls = [http.post.call_args_list[i][0][0] for i in range(http.post.call_count)]
    assert any(u.endswith("/pullrequests/9/approve") for u in urls)


@patch("code_review.providers.bitbucket.http_get_json_or_text")
def test_get_bot_blocking_state_needs_work(mock_get):
    mock_get.side_effect = [
        {"uuid": "{me}"},
        {
            "participants": [
                {"user": {"uuid": "{me}"}, "state": "needs_work"},
            ]
        },
    ]
    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    assert p.get_bot_blocking_state("ws", "repo", 1) == "BLOCKING"


@patch("code_review.providers.bitbucket.http_get_json_or_text")
def test_get_bot_blocking_state_approved(mock_get):
    mock_get.side_effect = [
        {"uuid": "{me}"},
        {
            "participants": [
                {"user": {"uuid": "{me}"}, "approved": True, "state": "approved"},
            ]
        },
    ]
    p = BitbucketProvider("https://api.bitbucket.org/2.0", "tok")
    assert p.get_bot_blocking_state("ws", "repo", 1) == "NOT_BLOCKING"
