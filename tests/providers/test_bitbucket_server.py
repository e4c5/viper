"""Tests for BitbucketServerProvider (mocked HTTP)."""

import pytest
from unittest.mock import MagicMock, call, patch

from code_review.providers import get_provider
from code_review.providers.base import InlineComment
from code_review.providers.bitbucket_server import (
    BitbucketServerProvider,
    _bitbucket_json_diff_to_unified,
    _extract_commit_id,
)


def test_get_provider_bitbucket_server():
    p = get_provider("bitbucket_server", "https://bb:7990/rest/api/1.0", "token")
    assert isinstance(p, BitbucketServerProvider)


# ---------------------------------------------------------------------------
# _bitbucket_json_diff_to_unified unit tests
# ---------------------------------------------------------------------------


def _make_bb_diff(src_path, dst_path, hunks):
    """Helper to build a minimal Bitbucket Server JSON diff dict."""
    return {
        "diffs": [
            {
                "source": {"toString": src_path} if src_path else None,
                "destination": {"toString": dst_path} if dst_path else None,
                "hunks": hunks,
            }
        ]
    }


def test_bitbucket_json_diff_to_unified_modified_file():
    """A modified file with CONTEXT, ADDED and REMOVED segments converts correctly."""
    data = _make_bb_diff(
        "src/Foo.java",
        "src/Foo.java",
        [
            {
                "sourceLine": 10,
                "sourceSpan": 3,
                "destinationLine": 10,
                "destinationSpan": 4,
                "segments": [
                    {"type": "CONTEXT", "lines": [{"line": "context line"}]},
                    {"type": "ADDED", "lines": [{"line": "new line"}, {"line": "another new"}]},
                    {"type": "REMOVED", "lines": [{"line": "old line"}]},
                    {"type": "CONTEXT", "lines": [{"line": "end context"}]},
                ],
            }
        ],
    )
    result = _bitbucket_json_diff_to_unified(data)
    lines = result.splitlines()
    assert lines[0] == "diff --git a/src/Foo.java b/src/Foo.java"
    assert lines[1] == "--- a/src/Foo.java"
    assert lines[2] == "+++ b/src/Foo.java"
    assert lines[3] == "@@ -10,3 +10,4 @@"
    assert lines[4] == " context line"
    assert lines[5] == "+new line"
    assert lines[6] == "+another new"
    assert lines[7] == "-old line"
    assert lines[8] == " end context"


def test_bitbucket_json_diff_to_unified_new_file():
    """A new file (no source) uses /dev/null as the source header."""
    data = _make_bb_diff(
        None,
        "src/NewFile.java",
        [
            {
                "sourceLine": 0,
                "sourceSpan": 0,
                "destinationLine": 1,
                "destinationSpan": 2,
                "segments": [
                    {"type": "ADDED", "lines": [{"line": "line 1"}, {"line": "line 2"}]},
                ],
            }
        ],
    )
    result = _bitbucket_json_diff_to_unified(data)
    lines = result.splitlines()
    assert lines[0] == "diff --git a/src/NewFile.java b/src/NewFile.java"
    assert lines[1] == "--- /dev/null"
    assert lines[2] == "+++ b/src/NewFile.java"
    assert lines[3] == "@@ -0,0 +1,2 @@"
    assert lines[4] == "+line 1"
    assert lines[5] == "+line 2"


def test_bitbucket_json_diff_to_unified_deleted_file():
    """A deleted file (no destination) uses /dev/null as the destination header."""
    data = _make_bb_diff(
        "src/Old.java",
        None,
        [
            {
                "sourceLine": 1,
                "sourceSpan": 2,
                "destinationLine": 0,
                "destinationSpan": 0,
                "segments": [
                    {"type": "REMOVED", "lines": [{"line": "gone 1"}, {"line": "gone 2"}]},
                ],
            }
        ],
    )
    result = _bitbucket_json_diff_to_unified(data)
    lines = result.splitlines()
    assert lines[0] == "diff --git a/src/Old.java b/src/Old.java"
    assert lines[1] == "--- a/src/Old.java"
    assert lines[2] == "+++ /dev/null"
    assert lines[3] == "@@ -1,2 +0,0 @@"
    assert lines[4] == "-gone 1"
    assert lines[5] == "-gone 2"


def test_bitbucket_json_diff_to_unified_empty_diffs():
    """An empty diffs array produces an empty string."""
    assert _bitbucket_json_diff_to_unified({"diffs": []}) == ""
    assert _bitbucket_json_diff_to_unified({}) == ""


def test_bitbucket_json_diff_to_unified_multiple_files():
    """Multiple files in a single diff response are all converted."""
    data = {
        "diffs": [
            {
                "source": {"toString": "a.py"},
                "destination": {"toString": "a.py"},
                "hunks": [
                    {
                        "sourceLine": 1,
                        "sourceSpan": 1,
                        "destinationLine": 1,
                        "destinationSpan": 2,
                        "segments": [
                            {"type": "CONTEXT", "lines": [{"line": "ctx"}]},
                            {"type": "ADDED", "lines": [{"line": "added"}]},
                        ],
                    }
                ],
            },
            {
                "source": {"toString": "b.py"},
                "destination": {"toString": "b.py"},
                "hunks": [
                    {
                        "sourceLine": 5,
                        "sourceSpan": 1,
                        "destinationLine": 5,
                        "destinationSpan": 1,
                        "segments": [
                            {"type": "REMOVED", "lines": [{"line": "removed"}]},
                            {"type": "ADDED", "lines": [{"line": "replaced"}]},
                        ],
                    }
                ],
            },
        ]
    }
    result = _bitbucket_json_diff_to_unified(data)
    assert "--- a/a.py" in result
    assert "+++ b/a.py" in result
    assert "--- a/b.py" in result
    assert "+++ b/b.py" in result
    assert "+added" in result
    assert "-removed" in result
    assert "+replaced" in result


# ---------------------------------------------------------------------------
# get_pr_diff — JSON diff handling
# ---------------------------------------------------------------------------


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_pr_diff_parses_bitbucket_json_response(mock_client):
    """get_pr_diff converts Bitbucket Server JSON diff to unified diff text."""
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "diffs": [
            {
                "source": {"toString": "src/Main.java"},
                "destination": {"toString": "src/Main.java"},
                "hunks": [
                    {
                        "sourceLine": 1,
                        "sourceSpan": 1,
                        "destinationLine": 1,
                        "destinationSpan": 2,
                        "segments": [
                            {"type": "CONTEXT", "lines": [{"line": "public class Main {"}]},
                            {"type": "ADDED", "lines": [{"line": "    // new comment"}]},
                        ],
                    }
                ],
            }
        ]
    }
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    diff = p.get_pr_diff("PROJ", "my-repo", 42)

    assert "--- a/src/Main.java" in diff
    assert "+++ b/src/Main.java" in diff
    assert " public class Main {" in diff
    assert "+    // new comment" in diff


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_pr_diff_returns_text_response_as_is(mock_client):
    """get_pr_diff passes through a plain-text diff unchanged."""
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "text/plain"}
    mock_resp.text = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    diff = p.get_pr_diff("PROJ", "my-repo", 1)

    assert diff == "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_pr_files_uses_json_diff(mock_client):
    """get_pr_files correctly extracts files when diff comes back as Bitbucket JSON."""
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "diffs": [
            {
                "source": {"toString": "src/Foo.java"},
                "destination": {"toString": "src/Foo.java"},
                "hunks": [
                    {
                        "sourceLine": 1,
                        "sourceSpan": 1,
                        "destinationLine": 1,
                        "destinationSpan": 2,
                        "segments": [
                            {"type": "ADDED", "lines": [{"line": "// added"}]},
                        ],
                    }
                ],
            },
            {
                "source": {"toString": "src/Bar.java"},
                "destination": {"toString": "src/Bar.java"},
                "hunks": [
                    {
                        "sourceLine": 5,
                        "sourceSpan": 1,
                        "destinationLine": 5,
                        "destinationSpan": 1,
                        "segments": [
                            {"type": "REMOVED", "lines": [{"line": "old"}]},
                            {"type": "ADDED", "lines": [{"line": "new"}]},
                        ],
                    }
                ],
            },
        ]
    }
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    files = p.get_pr_files("PROJ", "my-repo", 42)

    paths = [f.path for f in files]
    assert "src/Foo.java" in paths
    assert "src/Bar.java" in paths


# ---------------------------------------------------------------------------
# get_existing_review_comments uses /activities (not /comments)
# ---------------------------------------------------------------------------


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_existing_review_comments_uses_activities_endpoint(mock_client):
    """get_existing_review_comments must call /activities, not /comments.

    Bitbucket Server requires a 'path' query parameter for GET /comments and
    returns 400/404 without it.  The activities endpoint is the correct way to
    retrieve all PR comments.
    """
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "isLastPage": True,
        "values": [
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 1,
                    "text": "Looks good",
                    "state": "OPEN",
                    "anchor": {"path": "src/Foo.java", "line": 5},
                },
            }
        ],
    }
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    comments = p.get_existing_review_comments("PROJ", "my-repo", 42)

    # Verify the /activities URL was called, not /comments
    call_args = mock_client.return_value.__enter__.return_value.get.call_args
    called_url = call_args[0][0]
    assert called_url.endswith("/activities"), (
        f"Expected /activities endpoint, got: {called_url}"
    )
    assert "/comments" not in called_url

    assert len(comments) == 1
    assert comments[0].body == "Looks good"
    assert comments[0].path == "src/Foo.java"
    assert comments[0].line == 5


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

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    from_id, to_id = p._get_pr_diff_refs("PROJ", "my-repo", 42)
    assert from_id == "fromhash111"
    assert to_id == "tohash222"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_pr_diff_refs_returns_none_none_on_error(mock_client):
    """_get_pr_diff_refs returns (None, None) gracefully when the API call fails."""
    mock_client.return_value.__enter__.return_value.get.side_effect = RuntimeError("network error")

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    from_id, to_id = p._get_pr_diff_refs("PROJ", "my-repo", 1)
    assert from_id is None
    assert to_id is None


# ---------------------------------------------------------------------------
# post_review_comments — lineType correctness
# ---------------------------------------------------------------------------


def _setup_post_review_comments_mocks(mock_client):
    """Shared httpx.Client mocking for post_review_comments tests."""
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

    provider = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    return provider, http


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_post_review_comments_uses_line_type_added(mock_client):
    """ADDED lines must be posted with lineType='ADDED'."""
    provider, http = _setup_post_review_comments_mocks(mock_client)

    provider.post_review_comments(
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
    provider, http = _setup_post_review_comments_mocks(mock_client)

    provider.post_review_comments(
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

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
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
