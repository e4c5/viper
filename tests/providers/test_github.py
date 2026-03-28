"""Tests for GitHub provider (mocked HTTP)."""

import base64
from unittest.mock import MagicMock, patch

import httpx

from code_review.providers import get_provider
from code_review.providers.base import InlineComment
from code_review.providers.github import GitHubProvider


def test_get_provider_github():
    p = get_provider("github", "https://api.github.com", "token")
    assert isinstance(p, GitHubProvider)


@patch("code_review.providers.github.httpx.Client")
def test_get_pr_diff(mock_client):
    mock_resp = MagicMock()
    mock_resp.text = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"
    mock_resp.headers = {}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitHubProvider("https://api.github.com", "tok")
    diff = p.get_pr_diff("owner", "repo", 1)
    assert "diff --git" in diff
    call = mock_client.return_value.__enter__.return_value.get.call_args
    assert call[1]["headers"].get("Accept") == "application/vnd.github.v3.diff"


@patch("code_review.providers.github.httpx.Client")
def test_get_incremental_pr_diff_uses_compare_endpoint(mock_client):
    mock_resp = MagicMock()
    mock_resp.text = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"
    mock_resp.headers = {}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitHubProvider("https://api.github.com", "tok")
    diff = p.get_incremental_pr_diff("owner", "repo", 1, "base123", "head456")

    assert "diff --git" in diff
    call = mock_client.return_value.__enter__.return_value.get.call_args
    assert "/compare/base123...head456" in call[0][0]
    assert call[1]["headers"].get("Accept") == "application/vnd.github.v3.diff"


@patch("code_review.providers.github.httpx.Client")
def test_get_incremental_pr_diff_falls_back_to_full_pr_diff_on_compare_error(mock_client):
    compare_err = httpx.HTTPStatusError(
        "compare failed",
        request=MagicMock(),
        response=MagicMock(status_code=404),
    )
    full_pr_resp = MagicMock()
    full_pr_resp.text = "diff --git a/full.py b/full.py\n--- a/full.py\n+++ b/full.py"
    full_pr_resp.headers = {}
    mock_client.return_value.__enter__.return_value.get.side_effect = [compare_err, full_pr_resp]

    p = GitHubProvider("https://api.github.com", "tok")
    diff = p.get_incremental_pr_diff("owner", "repo", 1, "base123", "head456")

    assert "diff --git a/full.py b/full.py" in diff
    calls = mock_client.return_value.__enter__.return_value.get.call_args_list
    assert "/compare/base123...head456" in calls[0][0][0]
    assert "/pulls/1" in calls[1][0][0]


@patch("code_review.providers.github.httpx.Client")
def test_get_file_content(mock_client):
    content_b64 = base64.b64encode(b"print('hello')").decode()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"content": content_b64, "encoding": "base64"}
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitHubProvider("https://api.github.com", "tok")
    content = p.get_file_content("owner", "repo", "main", "foo.py")
    assert content == "print('hello')"


@patch("code_review.providers.github.httpx.Client")
def test_get_pr_commit_messages(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"commit": {"message": "first\n\nbody"}},
        {"commit": {"message": "second line"}},
    ]
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitHubProvider("https://api.github.com", "tok")
    msgs = p.get_pr_commit_messages("owner", "repo", 3)
    assert msgs == ["first\n\nbody", "second line"]
    call = mock_client.return_value.__enter__.return_value.get.call_args
    assert "/pulls/3/commits" in call[0][0]


@patch("code_review.providers.github.httpx.Client")
def test_get_pr_files(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"filename": "foo.py", "status": "modified", "additions": 5, "deletions": 2},
        {"filename": "bar.go", "status": "added", "additions": 10, "deletions": 0},
    ]
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitHubProvider("https://api.github.com", "tok")
    files = p.get_pr_files("owner", "repo", 1)
    assert len(files) == 2
    assert files[0].path == "foo.py"
    assert files[0].status == "modified"
    assert files[1].path == "bar.go"
    assert files[1].status == "added"


@patch("code_review.providers.github.httpx.Client")
def test_get_incremental_pr_files_uses_compare_endpoint(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "files": [
            {"filename": "foo.py", "status": "modified", "additions": 1, "deletions": 0}
        ]
    }
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitHubProvider("https://api.github.com", "tok")
    files = p.get_incremental_pr_files("owner", "repo", 1, "base123", "head456")

    assert len(files) == 1
    assert files[0].path == "foo.py"
    call = mock_client.return_value.__enter__.return_value.get.call_args
    assert "/compare/base123...head456" in call[0][0]


@patch("code_review.providers.github.httpx.Client")
def test_get_incremental_pr_files_fall_back_to_full_pr_files_on_compare_error(mock_client):
    compare_err = httpx.HTTPStatusError(
        "compare failed",
        request=MagicMock(),
        response=MagicMock(status_code=404),
    )
    full_pr_resp = MagicMock()
    full_pr_resp.json.return_value = [
        {"filename": "full.py", "status": "modified", "additions": 2, "deletions": 1}
    ]
    full_pr_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.side_effect = [compare_err, full_pr_resp]

    p = GitHubProvider("https://api.github.com", "tok")
    files = p.get_incremental_pr_files("owner", "repo", 1, "base123", "head456")

    assert len(files) == 1
    assert files[0].path == "full.py"
    calls = mock_client.return_value.__enter__.return_value.get.call_args_list
    assert "/compare/base123...head456" in calls[0][0][0]
    assert "/pulls/1/files" in calls[1][0][0]


@patch("code_review.providers.github.httpx.Client")
def test_post_review_comments(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.content = b""
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = GitHubProvider("https://api.github.com", "tok")
    p.post_review_comments(
        "owner",
        "repo",
        1,
        [InlineComment(path="foo.py", line=10, body="[High] Bug here")],
        head_sha="abc123",
    )
    call_args = mock_client.return_value.__enter__.return_value.post.call_args
    assert "/reviews" in call_args[0][0]
    payload = call_args[1]["json"]
    assert "comments" in payload
    assert payload["comments"] == [
        {"path": "foo.py", "line": 10, "side": "RIGHT", "body": "[High] Bug here"}
    ]
    assert payload["commit_id"] == "abc123"
    assert payload["event"] == "COMMENT"


@patch("code_review.providers.github.httpx.Client")
def test_post_review_comments_with_suggested_patch(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.content = b""
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = GitHubProvider("https://api.github.com", "tok")
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
        head_sha="abc123",
    )
    call_args = mock_client.return_value.__enter__.return_value.post.call_args
    payload = call_args[1]["json"]
    comment_body = payload["comments"][0]["body"]
    assert "[Medium] Consider refactor." in comment_body
    assert "```suggestion" in comment_body
    assert "replacement_code();" in comment_body


@patch("code_review.providers.github.httpx.Client")
def test_get_existing_review_comments(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"id": 1, "path": "foo.py", "line": 10, "body": "[High] Bug"},
        {"id": 2, "path": "bar.py", "line": 5, "body": "[Low] Nit"},
    ]
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitHubProvider("https://api.github.com", "tok")
    comments = p.get_existing_review_comments("owner", "repo", 1)
    assert len(comments) == 2
    assert comments[0].id == "1"
    assert comments[0].path == "foo.py"
    assert comments[0].line == 10
    assert comments[1].id == "2"
    # GitHub does not expose resolved on list; we default False
    assert comments[0].resolved is False


@patch("code_review.providers.github.httpx.Client")
def test_post_pr_summary_comment(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = {"id": 1}
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = GitHubProvider("https://api.github.com", "tok")
    p.post_pr_summary_comment("owner", "repo", 1, "Summary body")
    call_args = mock_client.return_value.__enter__.return_value.post.call_args
    assert "/issues/1/comments" in call_args[0][0]
    assert call_args[1]["json"] == {"body": "Summary body"}


@patch("code_review.providers.github.httpx.Client")
def test_submit_review_decision(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.content = b""
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = GitHubProvider("https://api.github.com", "tok")
    p.submit_review_decision(
        "owner",
        "repo",
        1,
        "REQUEST_CHANGES",
        body="Automated threshold decision",
        head_sha="abc123",
    )
    call_args = mock_client.return_value.__enter__.return_value.post.call_args
    assert "/repos/owner/repo/pulls/1/reviews" in call_args[0][0]
    assert call_args[1]["json"]["event"] == "REQUEST_CHANGES"
    assert call_args[1]["json"]["body"] == "Automated threshold decision"
    assert call_args[1]["json"]["commit_id"] == "abc123"


@patch("code_review.providers.github.httpx.Client")
def test_get_bot_blocking_state_unknown_when_list_reviews_fails(mock_client):
    """Failed reviews listing must not be treated as empty (NOT_BLOCKING)."""
    ok_user = MagicMock()
    ok_user.headers = {"content-type": "application/json"}
    ok_user.json.return_value = {"login": "thebot"}
    ok_user.raise_for_status = MagicMock()

    bad = MagicMock()
    bad.raise_for_status.side_effect = httpx.HTTPStatusError(
        "err",
        request=MagicMock(),
        response=MagicMock(status_code=503),
    )

    mock_client.return_value.__enter__.return_value.get.side_effect = [ok_user, bad]

    p = GitHubProvider("https://api.github.com", "tok")
    assert p.get_bot_blocking_state("owner", "repo", 1) == "UNKNOWN"


@patch("code_review.providers.github.httpx.Client")
def test_get_pr_info(mock_client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "title": "Fix bug",
        "labels": [{"name": "skip-review"}, {"name": "bug"}],
    }
    mock_resp.headers = {"content-type": "application/json"}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitHubProvider("https://api.github.com", "tok")
    info = p.get_pr_info("owner", "repo", 1)
    assert info is not None
    assert info.title == "Fix bug"
    assert "skip-review" in info.labels


def test_capabilities_support_review_decisions():
    p = GitHubProvider("https://api.github.com", "tok")
    caps = p.capabilities()
    assert caps.supports_review_decisions is True


@patch("code_review.providers.github.httpx.Client")
def test_get_unresolved_review_items_uses_graphql_threads(mock_client):
    """Unresolved quality gate uses reviewThreads; skips resolved and outdated."""
    gql = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "t1",
                                "isResolved": False,
                                "isOutdated": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 1,
                                            "body": "[High] Bug",
                                            "path": "a.py",
                                            "line": 2,
                                        }
                                    ]
                                },
                            },
                            {
                                "id": "t2",
                                "isResolved": True,
                                "isOutdated": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 2,
                                            "body": "[High] Skip",
                                            "path": "b.py",
                                            "line": 1,
                                        }
                                    ]
                                },
                            },
                            {
                                "id": "t3",
                                "isResolved": False,
                                "isOutdated": True,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 3,
                                            "body": "[High] Old",
                                            "path": "c.py",
                                            "line": 1,
                                        }
                                    ]
                                },
                            },
                        ],
                    }
                }
            }
        }
    }
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.json.return_value = gql
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = GitHubProvider("https://api.github.com", "tok")
    items = p.get_unresolved_review_items_for_quality_gate("owner", "repo", 7)
    assert len(items) == 1
    assert items[0].kind == "discussion_thread"
    assert items[0].inferred_severity == "high"
    assert items[0].path == "a.py"
    post_url = mock_client.return_value.__enter__.return_value.post.call_args[0][0]
    assert post_url == "https://api.github.com/graphql"


@patch.object(GitHubProvider, "_graphql")
def test_unresolved_review_threads_stops_on_repeated_end_cursor(mock_graphql):
    """Same endCursor with hasNextPage must not paginate forever."""
    page = {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "stuck"},
                    "nodes": [],
                }
            }
        }
    }
    mock_graphql.return_value = page
    p = GitHubProvider("https://api.github.com", "tok")
    assert p._unresolved_review_threads_graphql("owner", "repo", 3) == []
    assert mock_graphql.call_count == 2


@patch("code_review.providers.github.httpx.Client")
def test_get_unresolved_review_items_graphql_failure_returns_empty(mock_client):
    """GraphQL failure must not reclassify all REST review comments as unresolved."""
    mock_post = MagicMock()
    mock_post.raise_for_status.side_effect = httpx.HTTPStatusError(
        "err", request=MagicMock(), response=MagicMock(status_code=500)
    )
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post

    p = GitHubProvider("https://api.github.com", "tok")
    items = p.get_unresolved_review_items_for_quality_gate("o", "r", 1)
    assert items == []
    mock_client.return_value.__enter__.return_value.get.assert_not_called()


@patch("code_review.providers.github.GitHubProvider._graphql")
def test_get_review_thread_dismissal_context_finds_thread(mock_gql):
    mock_gql.return_value = {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "PRRT_kwDOABC",
                            "isResolved": False,
                            "isOutdated": False,
                            "comments": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "databaseId": 100,
                                        "body": "[High] x",
                                        "path": "a.py",
                                        "line": 1,
                                        "createdAt": "t1",
                                        "author": {"login": "bot"},
                                    },
                                    {
                                        "databaseId": 200,
                                        "body": "fixed",
                                        "path": "a.py",
                                        "line": 1,
                                        "createdAt": "t2",
                                        "author": {"login": "dev"},
                                    },
                                ],
                            },
                        }
                    ],
                }
            }
        }
    }
    p = GitHubProvider("https://api.github.com", "tok")
    ctx = p.get_review_thread_dismissal_context("o", "r", 1, "200")
    assert ctx is not None
    assert ctx.gate_exclusion_stable_id == "github:thread:PRRT_kwDOABC"
    assert ctx.thread_id == "PRRT_kwDOABC"
    assert len(ctx.entries) == 2


@patch.object(GitHubProvider, "_graphql")
def test_get_review_thread_dismissal_context_fetches_extra_comment_pages(mock_gql):
    """Triggered comment past first comments page is found via node(id) pagination."""
    first_page_nodes = [
        {
            "databaseId": 1000 + i,
            "body": f"c{i}",
            "path": "a.py",
            "line": 1,
            "createdAt": "t0",
            "author": {"login": "u"},
        }
        for i in range(50)
    ]
    thread = {
        "id": "PRRT_kwLONG",
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "pageInfo": {"hasNextPage": True, "endCursor": "commentCur1"},
            "nodes": first_page_nodes,
        },
    }
    list_threads = {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [thread],
                }
            }
        }
    }
    more_comments = {
        "node": {
            "comments": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "databaseId": 9999,
                        "body": "target",
                        "path": "a.py",
                        "line": 1,
                        "createdAt": "t9",
                        "author": {"login": "human"},
                    }
                ],
            }
        }
    }
    mock_gql.side_effect = [list_threads, more_comments]

    p = GitHubProvider("https://api.github.com", "tok")
    ctx = p.get_review_thread_dismissal_context("o", "r", 1, "9999")
    assert ctx is not None
    assert ctx.thread_id == "PRRT_kwLONG"
    assert len(ctx.entries) == 51
    assert ctx.entries[-1].comment_id == "9999"


@patch("code_review.providers.github.httpx.Client")
def test_get_bot_attribution_identity_github(mock_client):
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"login": "MyBot", "id": 42}
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = GitHubProvider("https://api.github.com", "tok")
    bid = p.get_bot_attribution_identity("o", "r", 1)
    assert bid.login == "mybot"
    assert bid.id_str == "42"


@patch.object(GitHubProvider, "_post")
def test_post_review_thread_reply_github(mock_post):
    p = GitHubProvider("https://api.github.com", "tok")
    p.post_review_thread_reply("o", "r", 1, "99", "hello")
    mock_post.assert_called_once_with(
        "/repos/o/r/pulls/1/comments",
        {"body": "hello", "in_reply_to": 99},
    )


@patch.object(GitHubProvider, "_graphql")
def test_resolve_review_thread_github(mock_gql):
    from code_review.schemas.review_thread_dismissal import ReviewThreadDismissalContext

    p = GitHubProvider("https://api.github.com", "tok")
    p.resolve_review_thread(
        "o",
        "r",
        1,
        ReviewThreadDismissalContext(
            gate_exclusion_stable_id="github:thread:PRRT_kwDOABC",
            thread_id="PRRT_kwDOABC",
            entries=[],
        ),
        "200",
    )
    mock_gql.assert_called_once_with(
        GitHubProvider._RESOLVE_REVIEW_THREAD_GQL,
        {"threadId": "PRRT_kwDOABC"},
    )
