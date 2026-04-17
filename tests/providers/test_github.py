"""Tests for GitHub provider."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from github.GithubException import GithubException

from code_review.providers import get_provider
from code_review.providers.base import InlineComment
from code_review.providers.github import GitHubProvider


def _fake_file(
    filename: str,
    *,
    status: str = "modified",
    additions: int = 0,
    deletions: int = 0,
    patch: str = "",
    previous_filename: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        filename=filename,
        status=status,
        additions=additions,
        deletions=deletions,
        patch=patch,
        previous_filename=previous_filename,
    )


def _fake_comment(comment_id: int, path: str, line: int, body: str) -> SimpleNamespace:
    return SimpleNamespace(id=comment_id, path=path, line=line, body=body)


def _fake_review(review_id: int, state: str, login: str) -> SimpleNamespace:
    return SimpleNamespace(id=review_id, state=state, user=SimpleNamespace(login=login))


def test_get_provider_github():
    p = get_provider("github", "https://api.github.com", "token")
    assert isinstance(p, GitHubProvider)


def test_get_pr_diff():
    client = MagicMock()
    client.request_text.return_value = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        diff = p.get_pr_diff("owner", "repo", 1)
    assert "diff --git" in diff
    client.request_text.assert_called_once_with(
        "GET",
        "/repos/owner/repo/pulls/1",
        headers={"Accept": "application/vnd.github.v3.diff"},
    )


def test_get_incremental_pr_diff_uses_compare_endpoint():
    client = MagicMock()
    repo = MagicMock()
    repo.compare.return_value = SimpleNamespace(files=[_fake_file("foo.py")])
    client.get_repo.return_value = repo
    client.request_text.return_value = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        diff = p.get_incremental_pr_diff("owner", "repo", 1, "base123", "head456")

    assert "diff --git" in diff
    repo.compare.assert_called_once_with("base123", "head456")
    client.request_text.assert_called_once_with(
        "GET",
        "/repos/owner/repo/compare/base123...head456",
        headers={"Accept": "application/vnd.github.v3.diff"},
    )


def test_get_incremental_pr_diff_falls_back_to_full_pr_diff_on_compare_error():
    client = MagicMock()
    repo = MagicMock()
    repo.compare.side_effect = GithubException(404, {"message": "compare failed"})
    client.get_repo.return_value = repo
    client.request_text.return_value = (
        "diff --git a/full.py b/full.py\n--- a/full.py\n+++ b/full.py"
    )
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        diff = p.get_incremental_pr_diff("owner", "repo", 1, "base123", "head456")

    assert "diff --git a/full.py b/full.py" in diff
    client.request_text.assert_called_once_with(
        "GET",
        "/repos/owner/repo/pulls/1",
        headers={"Accept": "application/vnd.github.v3.diff"},
    )


def test_get_incremental_pr_diff_falls_back_to_full_pr_diff_when_compare_files_truncate():
    client = MagicMock()
    repo = MagicMock()
    repo.compare.return_value = SimpleNamespace(
        files=[_fake_file(f"file_{index}.py") for index in range(300)]
    )
    client.get_repo.return_value = repo
    client.request_text.return_value = (
        "diff --git a/full.py b/full.py\n--- a/full.py\n+++ b/full.py"
    )
    p = GitHubProvider("https://api.github.com", "tok")

    with patch.object(GitHubProvider, "_client", return_value=client):
        diff = p.get_incremental_pr_diff("owner", "repo", 1, "base123", "head456")

    assert "diff --git a/full.py b/full.py" in diff
    client.request_text.assert_called_once_with(
        "GET",
        "/repos/owner/repo/pulls/1",
        headers={"Accept": "application/vnd.github.v3.diff"},
    )


def test_get_file_content():
    client = MagicMock()
    repo = MagicMock()
    repo.get_contents.return_value = SimpleNamespace(decoded_content=b"print('hello')")
    client.get_repo.return_value = repo
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        content = p.get_file_content("owner", "repo", "main", "foo.py")
    assert content == "print('hello')"
    repo.get_contents.assert_called_once_with("foo.py", ref="main")


def test_get_pr_commit_messages():
    client = MagicMock()
    pull = MagicMock()
    pull.get_commits.return_value = [
        SimpleNamespace(commit=SimpleNamespace(message="first\n\nbody"), raw_data={}),
        SimpleNamespace(commit=SimpleNamespace(message="second line"), raw_data={}),
    ]
    client.get_pull.return_value = pull
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        msgs = p.get_pr_commit_messages("owner", "repo", 3)
    assert msgs == ["first\n\nbody", "second line"]


def test_get_pr_files():
    client = MagicMock()
    pull = MagicMock()
    pull.get_files.return_value = [
        _fake_file("foo.py", status="modified", additions=5, deletions=2),
        _fake_file("bar.go", status="added", additions=10, deletions=0),
    ]
    client.get_pull.return_value = pull
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        files = p.get_pr_files("owner", "repo", 1)
    assert len(files) == 2
    assert files[0].path == "foo.py"
    assert files[0].status == "modified"
    assert files[1].path == "bar.go"
    assert files[1].status == "added"


def test_get_pr_diff_for_file_uses_patch_from_matching_file():
    client = MagicMock()
    pull = MagicMock()
    pull.get_files.return_value = [
        _fake_file(
            "src/Foo.java",
            status="modified",
            patch="@@ -4,2 +4,2 @@\n-old\n+new",
        )
    ]
    client.get_pull.return_value = pull
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        diff = p.get_pr_diff_for_file("owner", "repo", 1, "src/Foo.java")

    assert "diff --git a/src/Foo.java b/src/Foo.java" in diff
    assert "@@ -4,2 +4,2 @@" in diff


def test_get_pr_diff_for_file_falls_back_to_full_diff_when_github_omits_patch():
    client = MagicMock()
    pull = MagicMock()
    pull.get_files.return_value = [_fake_file("src/Foo.java", status="modified")]
    client.get_pull.return_value = pull
    full_diff = (
        "diff --git a/src/Foo.java b/src/Foo.java\n"
        "--- a/src/Foo.java\n"
        "+++ b/src/Foo.java\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        with patch.object(
            GitHubProvider, "get_pr_diff", return_value=full_diff
        ) as mock_get_pr_diff:
            diff = p.get_pr_diff_for_file("owner", "repo", 1, "src/Foo.java")

    assert "diff --git a/src/Foo.java b/src/Foo.java" in diff
    assert "+new" in diff
    mock_get_pr_diff.assert_called_once_with("owner", "repo", 1)


def test_get_incremental_pr_files_uses_compare_endpoint():
    client = MagicMock()
    repo = MagicMock()
    repo.compare.return_value = SimpleNamespace(
        files=[_fake_file("foo.py", status="modified", additions=1, deletions=0)]
    )
    client.get_repo.return_value = repo
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        files = p.get_incremental_pr_files("owner", "repo", 1, "base123", "head456")

    assert len(files) == 1
    assert files[0].path == "foo.py"
    repo.compare.assert_called_once_with("base123", "head456")


def test_get_incremental_pr_files_fall_back_to_full_pr_files_on_compare_error():
    client = MagicMock()
    repo = MagicMock()
    repo.compare.side_effect = GithubException(404, {"message": "compare failed"})
    client.get_repo.return_value = repo
    pull = MagicMock()
    pull.get_files.return_value = [
        _fake_file("full.py", status="modified", additions=2, deletions=1)
    ]
    client.get_pull.return_value = pull
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        files = p.get_incremental_pr_files("owner", "repo", 1, "base123", "head456")

    assert len(files) == 1
    assert files[0].path == "full.py"


def test_get_incremental_pr_files_fall_back_to_full_pr_files_when_compare_files_truncate():
    client = MagicMock()
    repo = MagicMock()
    repo.compare.return_value = SimpleNamespace(
        files=[_fake_file(f"file_{index}.py", status="modified") for index in range(300)]
    )
    client.get_repo.return_value = repo
    pull = MagicMock()
    pull.get_files.return_value = [
        _fake_file("full.py", status="modified", additions=2, deletions=1)
    ]
    client.get_pull.return_value = pull
    p = GitHubProvider("https://api.github.com", "tok")

    with patch.object(GitHubProvider, "_client", return_value=client):
        files = p.get_incremental_pr_files("owner", "repo", 1, "base123", "head456")

    assert len(files) == 1
    assert files[0].path == "full.py"


def test_post_review_comments():
    client = MagicMock()
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        p.post_review_comments(
            "owner",
            "repo",
            1,
            [InlineComment(path="foo.py", line=10, body="[High] Bug here")],
            head_sha="abc123",
        )
    payload = client.create_pull_review.call_args.kwargs
    assert payload["comments"] == [
        {"path": "foo.py", "line": 10, "side": "RIGHT", "body": "[High] Bug here"}
    ]
    assert payload["event"] == "COMMENT"
    assert payload["body"] == "Code review comments"
    assert payload["head_sha"] == "abc123"


def test_post_review_comments_with_suggested_patch():
    client = MagicMock()
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
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
    comment_body = client.create_pull_review.call_args.kwargs["comments"][0]["body"]
    assert "[Medium] Consider refactor." in comment_body
    assert "```suggestion" in comment_body
    assert "replacement_code();" in comment_body


def test_get_existing_review_comments():
    client = MagicMock()
    pull = MagicMock()
    pull.get_review_comments.return_value = [
        _fake_comment(1, "foo.py", 10, "[High] Bug"),
        _fake_comment(2, "bar.py", 5, "[Low] Nit"),
    ]
    client.get_pull.return_value = pull
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        comments = p.get_existing_review_comments("owner", "repo", 1)
    assert len(comments) == 2
    assert comments[0].id == "1"
    assert comments[0].path == "foo.py"
    assert comments[0].line == 10
    assert comments[1].id == "2"
    # GitHub does not expose resolved on list; we default False
    assert comments[0].resolved is False


def test_post_pr_summary_comment():
    client = MagicMock()
    issue = MagicMock()
    client.get_issue.return_value = issue
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        p.post_pr_summary_comment("owner", "repo", 1, "Summary body")
    issue.create_comment.assert_called_once_with("Summary body")


def test_submit_review_decision():
    client = MagicMock()
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        p.submit_review_decision(
            "owner",
            "repo",
            1,
            "REQUEST_CHANGES",
            body="Automated threshold decision",
            head_sha="abc123",
        )
    client.create_pull_review.assert_called_once_with(
        "owner",
        "repo",
        1,
        event="REQUEST_CHANGES",
        body="Automated threshold decision",
        head_sha="abc123",
    )


def test_get_bot_blocking_state_unknown_when_list_reviews_fails():
    """Failed reviews listing must not be treated as empty (NOT_BLOCKING)."""
    client = MagicMock()
    client.get_authenticated_user.return_value = SimpleNamespace(login="thebot")
    client.get_pull.return_value.get_reviews.side_effect = RuntimeError("boom")
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        assert p.get_bot_blocking_state("owner", "repo", 1) == "UNKNOWN"


def test_get_pr_info():
    client = MagicMock()
    client.get_pull.return_value = SimpleNamespace(
        title="Fix bug",
        body="PR body",
        labels=[SimpleNamespace(name="skip-review"), SimpleNamespace(name="bug")],
        head=SimpleNamespace(sha="abc123"),
    )
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        info = p.get_pr_info("owner", "repo", 1)
    assert info is not None
    assert info.title == "Fix bug"
    assert "skip-review" in info.labels
    assert info.description == "PR body"
    assert info.head_sha == "abc123"


def test_capabilities_support_review_decisions():
    p = GitHubProvider("https://api.github.com", "tok")
    caps = p.capabilities()
    assert caps.supports_review_decisions is True


def test_get_unresolved_review_items_uses_graphql_threads():
    """Unresolved quality gate uses reviewThreads; skips resolved and outdated."""
    client = MagicMock()
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
    client.graphql_query.return_value = gql

    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        items = p.get_unresolved_review_items_for_quality_gate("owner", "repo", 7)
    assert len(items) == 1
    assert items[0].kind == "discussion_thread"
    assert items[0].inferred_severity == "high"
    assert items[0].path == "a.py"
    call = client.graphql_query.call_args
    assert call.args[0] == GitHubProvider._REVIEW_THREADS_GQL
    assert call.args[1] == {"owner": "owner", "name": "repo", "number": 7, "cursor": None}


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


def test_get_unresolved_review_items_graphql_failure_returns_empty():
    """GraphQL failure must not reclassify all REST review comments as unresolved."""
    client = MagicMock()
    client.graphql_query.side_effect = GithubException(500, {"message": "err"})

    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        items = p.get_unresolved_review_items_for_quality_gate("o", "r", 1)
    assert items == []
    client.get_pull.assert_not_called()


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
    assert ctx.path == "a.py"
    assert ctx.line == 1
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


def test_get_bot_attribution_identity_github():
    client = MagicMock()
    client.get_authenticated_user.return_value = SimpleNamespace(login="MyBot", id=42)
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        bid = p.get_bot_attribution_identity("o", "r", 1)
    assert bid.login == "mybot"
    assert bid.id_str == "42"


def test_post_review_thread_reply_github():
    client = MagicMock()
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        p.post_review_thread_reply("o", "r", 1, "99", "hello")
    client.reply_to_review_comment.assert_called_once_with(
        "o",
        "r",
        1,
        99,
        "hello",
    )


def test_update_pr_description_edits_body_and_optional_title():
    client = MagicMock()
    pull = MagicMock()
    client.get_pull.return_value = pull
    p = GitHubProvider("https://api.github.com", "tok")
    with patch.object(GitHubProvider, "_client", return_value=client):
        p.update_pr_description("o", "r", 1, "new body")
        p.update_pr_description("o", "r", 1, "other body", title="new title")
    assert pull.edit.call_args_list[0].kwargs == {"body": "new body"}
    assert pull.edit.call_args_list[1].kwargs == {"title": "new title", "body": "other body"}


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
