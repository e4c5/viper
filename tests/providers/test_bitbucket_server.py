"""Tests for BitbucketServerProvider (mocked HTTP)."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

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
def test_get_incremental_pr_diff_uses_compare_endpoint(mock_client):
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
                        "segments": [{"type": "ADDED", "lines": [{"line": "// new comment"}]}],
                    }
                ],
            }
        ]
    }
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    diff = p.get_incremental_pr_diff("PROJ", "my-repo", 42, "base123", "head456")

    assert "+// new comment" in diff
    call = mock_client.return_value.__enter__.return_value.get.call_args
    assert call[0][0].endswith("/compare/diff")
    assert call[1]["params"] == {"from": "base123", "to": "head456"}


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


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_incremental_pr_files_parse_compare_diff(mock_client):
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
                        "segments": [{"type": "ADDED", "lines": [{"line": "// added"}]}],
                    }
                ],
            }
        ]
    }
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    files = p.get_incremental_pr_files("PROJ", "my-repo", 42, "base123", "head456")

    assert len(files) == 1
    assert files[0].path == "src/Foo.java"


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
    assert called_url.endswith("/activities"), f"Expected /activities endpoint, got: {called_url}"
    assert "/comments" not in called_url

    assert len(comments) == 1
    assert comments[0].body == "Looks good"
    assert comments[0].path == "src/Foo.java"
    assert comments[0].line == 5


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_unresolved_review_items_merges_comments_and_open_tasks(mock_client):
    """Quality gate includes non-RESOLVED activity comments plus OPEN PR tasks."""

    def _get_side_effect(url: str, params=None, **kwargs):
        mock_r = MagicMock()
        mock_r.headers = {"content-type": "application/json"}
        mock_r.raise_for_status = MagicMock()
        u = str(url)
        if "/activities" in u:
            mock_r.json.return_value = {
                "isLastPage": True,
                "values": [
                    {
                        "action": "COMMENTED",
                        "comment": {
                            "id": 1,
                            "text": "[Low] note",
                            "state": "OPEN",
                            "anchor": {"path": "f.java", "line": 2},
                        },
                    }
                ],
            }
        elif "/tasks" in u:
            mock_r.json.return_value = {
                "isLastPage": True,
                "values": [{"id": 9, "state": "OPEN", "text": "[Medium] task body"}],
            }
        else:
            mock_r.json.return_value = {}
        return mock_r

    mock_client.return_value.__enter__.return_value.get.side_effect = _get_side_effect

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    items = p.get_unresolved_review_items_for_quality_gate("PROJ", "my-repo", 42)
    kinds = {i.kind for i in items}
    assert "inline_comment" in kinds
    assert "task" in kinds
    assert any(i.inferred_severity == "medium" for i in items)


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_unresolved_review_items_continues_when_activities_fails(mock_client):
    """Comment/activities fetch must not skip task-based quality gate signals."""

    def _get_side_effect(url: str, params=None, **kwargs):
        mock_r = MagicMock()
        mock_r.headers = {"content-type": "application/json"}
        u = str(url)
        if "/activities" in u:
            mock_r.raise_for_status.side_effect = httpx.HTTPStatusError(
                "503", request=MagicMock(), response=MagicMock()
            )
        elif "/tasks" in u:
            mock_r.raise_for_status = MagicMock()
            mock_r.json.return_value = {
                "isLastPage": True,
                "values": [{"id": 9, "state": "OPEN", "text": "[High] fix me"}],
            }
        else:
            mock_r.raise_for_status = MagicMock()
            mock_r.json.return_value = {}
        return mock_r

    mock_client.return_value.__enter__.return_value.get.side_effect = _get_side_effect

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    items = p.get_unresolved_review_items_for_quality_gate("PROJ", "my-repo", 42)
    assert len(items) == 1
    assert items[0].kind == "task"
    assert items[0].inferred_severity == "high"


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
    assert (
        _extract_commit_id({"id": "refs/heads/main", "latestCommit": bad_latest})
        == "refs/heads/main"
    )


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
        "PROJ",
        "repo",
        1,
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
        "PROJ",
        "repo",
        1,
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
        "PROJ",
        "repo",
        1,
        [InlineComment(path="foo.java", line=10, body="Bug", line_type="ADDED")],
        head_sha="source_head_hash",
    )
    payload = http.post.call_args[1]["json"]
    assert payload["anchor"]["fileType"] == "TO"
    assert payload["anchor"]["fromHash"] == "target_base_hash"
    assert payload["anchor"]["toHash"] == "source_head_hash"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_post_review_comments_retries_without_hashes_on_409(mock_client):
    """When the first POST returns 409, retry with simplified anchor (no fromHash/toHash/diffType).

    The 409 occurs when toRef.latestCommit != the PR's merge-base (i.e. the target branch
    has advanced after the PR was created).  The retry lets Bitbucket Server resolve the
    merge-base itself, which succeeds because only path/line/lineType/fileType are required.
    """
    # Simulate 409 on first POST, success on second (retry)
    mock_response_409 = MagicMock()
    mock_response_409.status_code = 409
    exc_409 = httpx.HTTPStatusError("409", request=MagicMock(), response=mock_response_409)

    mock_post_success = MagicMock()
    mock_post_success.raise_for_status = MagicMock()
    mock_post_success.content = b'{"id": 2}'
    mock_post_success.json.return_value = {"id": 2}

    mock_post_first = MagicMock()
    mock_post_first.raise_for_status.side_effect = exc_409

    mock_get = MagicMock()
    mock_get.headers = {"content-type": "application/json"}
    mock_get.json.return_value = {
        "fromRef": {"latestCommit": "source_head_hash"},
        "toRef": {"latestCommit": "target_base_hash"},
    }

    http = mock_client.return_value.__enter__.return_value
    http.get.return_value = mock_get
    http.post.side_effect = [mock_post_first, mock_post_success]

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    p.post_review_comments(
        "PROJ",
        "repo",
        1,
        [InlineComment(path="foo.java", line=10, body="Bug", line_type="ADDED")],
        head_sha="source_head_hash",
    )

    assert http.post.call_count == 2, "Should have retried once after 409"

    # First call had hashes
    first_payload = http.post.call_args_list[0][1]["json"]
    assert "fromHash" in first_payload["anchor"]
    assert "toHash" in first_payload["anchor"]
    assert "diffType" in first_payload["anchor"]

    # Retry has NO hashes but preserves essential anchor fields
    retry_payload = http.post.call_args_list[1][1]["json"]
    assert "fromHash" not in retry_payload["anchor"], "Retry must omit fromHash"
    assert "toHash" not in retry_payload["anchor"], "Retry must omit toHash"
    assert "diffType" not in retry_payload["anchor"], "Retry must omit diffType"
    assert retry_payload["anchor"]["path"] == "foo.java"
    assert retry_payload["anchor"]["line"] == 10
    assert retry_payload["anchor"]["lineType"] == "ADDED"
    assert retry_payload["anchor"]["fileType"] == "TO"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_post_review_comments_409_without_hashes_propagates(mock_client):
    """When there are no hashes and the POST returns 409, the error propagates (no retry loop)."""
    mock_response_409 = MagicMock()
    mock_response_409.status_code = 409
    exc_409 = httpx.HTTPStatusError("409", request=MagicMock(), response=mock_response_409)

    mock_post = MagicMock()
    mock_post.raise_for_status.side_effect = exc_409

    mock_get = MagicMock()
    mock_get.headers = {"content-type": "application/json"}
    # Return empty refs so no hashes are included in the anchor
    mock_get.json.return_value = {"fromRef": {}, "toRef": {}}

    http = mock_client.return_value.__enter__.return_value
    http.get.return_value = mock_get
    http.post.return_value = mock_post

    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    with pytest.raises(httpx.HTTPStatusError):
        p.post_review_comments(
            "PROJ",
            "repo",
            1,
            [InlineComment(path="foo.java", line=10, body="Bug", line_type="ADDED")],
        )

    # Only one POST attempt (no retry since there were no hashes to remove)
    assert http.post.call_count == 1


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


def test_capabilities():
    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    caps = p.capabilities()
    assert caps.supports_suggestions is True
    assert caps.supports_review_decisions is False
    assert caps.supports_review_thread_dismissal_context is True
    assert caps.supports_review_thread_reply is True


def test_capabilities_with_participant_slug_enables_review_decisions():
    p = BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0",
        "tok",
        participant_user_slug="buildbot",
    )
    assert p.capabilities().supports_review_decisions is True


def test_bbs_build_dismissal_context_thread():
    raw = [
        {"id": 1, "text": "root [High]", "author": {"name": "bot"}, "createdDate": 100},
        {
            "id": 2,
            "text": "reply",
            "parentComment": {"id": 1},
            "author": {"name": "dev"},
            "createdDate": 200,
        },
    ]
    ctx = BitbucketServerProvider._bbs_build_dismissal_context(raw, "2")
    assert ctx is not None
    assert ctx.gate_exclusion_stable_id == "comment:1"
    assert len(ctx.entries) == 2


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_review_thread_dismissal_context_server(mock_client):
    mock_r = MagicMock()
    mock_r.headers = {"content-type": "application/json"}
    mock_r.raise_for_status = MagicMock()
    mock_r.json.return_value = {
        "isLastPage": True,
        "values": [
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 10,
                    "text": "top",
                    "state": "OPEN",
                    "author": {"name": "a"},
                    "createdDate": 1,
                    "anchor": {"path": "f.java", "line": 1},
                },
            },
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 11,
                    "text": "child",
                    "state": "OPEN",
                    "parentComment": {"id": 10},
                    "author": {"name": "b"},
                    "createdDate": 2,
                    "anchor": {"path": "f.java", "line": 1},
                },
            },
        ],
    }
    mock_client.return_value.__enter__.return_value.get.return_value = mock_r
    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    ctx = p.get_review_thread_dismissal_context("PROJ", "repo", 7, "11")
    assert ctx is not None
    assert ctx.gate_exclusion_stable_id == "comment:10"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_post_review_thread_reply_bitbucket_server(mock_client):
    mock_post = MagicMock()
    mock_post.raise_for_status = MagicMock()
    mock_post.content = b""
    mock_client.return_value.__enter__.return_value.post.return_value = mock_post
    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    p.post_review_thread_reply("PROJ", "repo", 3, "99", "Please address")
    call = mock_client.return_value.__enter__.return_value.post.call_args
    assert "/pull-requests/3/comments" in call[0][0]
    assert call[1]["json"]["parent"]["id"] == 99
    assert call[1]["json"]["text"] == "Please address"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_submit_review_decision_needs_work(mock_client):
    mock_get = MagicMock()
    mock_get.raise_for_status = MagicMock()
    mock_get.headers = {"content-type": "application/json"}
    mock_get.json.return_value = {"version": 3, "id": 1}

    mock_put = MagicMock()
    mock_put.raise_for_status = MagicMock()
    mock_put.content = b""

    client = mock_client.return_value.__enter__.return_value
    client.get.return_value = mock_get
    client.put.return_value = mock_put

    p = BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0",
        "tok",
        participant_user_slug="buildbot",
    )
    p.submit_review_decision(
        "PROJ",
        "repo",
        42,
        "REQUEST_CHANGES",
        body="reason",
        head_sha="ignored",
    )
    assert client.put.call_count == 1
    put_args = client.put.call_args
    assert "/pull-requests/42/participants/" in put_args[0][0]
    assert "buildbot" in put_args[0][0]
    assert "version=3" in put_args[0][0]
    assert put_args[1]["json"] == {"status": "NEEDS_WORK"}


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_submit_review_decision_retries_participant_put_on_409(mock_client):
    mock_get_ok = MagicMock()
    mock_get_ok.raise_for_status = MagicMock()
    mock_get_ok.headers = {"content-type": "application/json"}
    mock_get_ok.json.side_effect = [{"version": 2}, {"version": 5}]

    req = httpx.Request("PUT", "https://bb/pull")
    resp_409 = httpx.Response(409, request=req)

    def raise_409() -> None:
        raise httpx.HTTPStatusError("conflict", request=req, response=resp_409)

    mock_put_fail = MagicMock()
    mock_put_fail.raise_for_status = raise_409
    mock_put_ok = MagicMock()
    mock_put_ok.raise_for_status = MagicMock()
    mock_put_ok.content = b""

    client = mock_client.return_value.__enter__.return_value
    client.get.return_value = mock_get_ok
    client.put.side_effect = [mock_put_fail, mock_put_ok]

    p = BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0",
        "tok",
        participant_user_slug="u1",
    )
    p.submit_review_decision("PROJ", "repo", 7, "APPROVE")

    assert client.get.call_count == 2
    assert client.put.call_count == 2
    assert "version=2" in client.put.call_args_list[0][0][0]
    assert "version=5" in client.put.call_args_list[1][0][0]


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_submit_review_decision_400_then_version_minus_one(mock_client):
    """Some Bitbucket builds reject ?version=0; wildcard -1 succeeds."""
    mock_get = MagicMock()
    mock_get.raise_for_status = MagicMock()
    mock_get.headers = {"content-type": "application/json"}
    mock_get.json.return_value = {"version": 0, "id": 1}

    req = httpx.Request("PUT", "https://bb/pull")
    resp_400 = httpx.Response(400, request=req, text="invalid version")
    put_calls = {"n": 0}

    def put_side_effect(*_a, **_kw):
        put_calls["n"] += 1
        if put_calls["n"] == 1:
            raise httpx.HTTPStatusError("bad", request=req, response=resp_400)
        m = MagicMock()
        m.raise_for_status = MagicMock()
        return m

    client = mock_client.return_value.__enter__.return_value
    client.get.return_value = mock_get
    client.put.side_effect = put_side_effect

    p = BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0",
        "tok",
        participant_user_slug="u1",
    )
    p.submit_review_decision("PROJ", "repo", 7, "REQUEST_CHANGES")

    assert client.get.call_count == 2
    assert put_calls["n"] == 2
    assert "version=-1" in client.put.call_args_list[-1][0][0]


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_submit_review_decision_400_then_refetched_version(mock_client):
    """HTTP 400 then refetch shows newer version; retry succeeds."""
    mock_get_ok = MagicMock()
    mock_get_ok.raise_for_status = MagicMock()
    mock_get_ok.headers = {"content-type": "application/json"}
    mock_get_ok.json.side_effect = [{"version": 0}, {"version": 4}]

    req = httpx.Request("PUT", "https://bb/pull")
    resp_400 = httpx.Response(400, request=req)

    mock_put_fail = MagicMock()
    mock_put_fail.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("bad", request=req, response=resp_400)
    )
    mock_put_ok = MagicMock()
    mock_put_ok.raise_for_status = MagicMock()
    mock_put_ok.content = b""

    client = mock_client.return_value.__enter__.return_value
    client.get.return_value = mock_get_ok
    client.put.side_effect = [mock_put_fail, mock_put_ok]

    p = BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0",
        "tok",
        participant_user_slug="u1",
    )
    p.submit_review_decision("PROJ", "repo", 7, "APPROVE")

    assert client.get.call_count == 2
    assert "version=4" in client.put.call_args_list[-1][0][0]


def test_submit_review_decision_requires_participant_slug():
    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    with pytest.raises(ValueError, match="SCM_BITBUCKET_SERVER_USER_SLUG"):
        p.submit_review_decision("PROJ", "repo", 1, "APPROVE")


def test_get_bot_blocking_state_unknown_without_participant_slug():
    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    assert p.get_bot_blocking_state("PROJ", "repo", 1) == "UNKNOWN"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_bot_blocking_state_unknown_when_pr_has_no_participant_or_reviewer_lists(mock_client):
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"id": 5, "title": "x"}
    mock_resp.raise_for_status = MagicMock()
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0",
        "tok",
        participant_user_slug="u1",
    )
    assert p.get_bot_blocking_state("PROJ", "repo", 5) == "UNKNOWN"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_bot_blocking_state_not_blocking_when_participants_and_reviewers_empty(mock_client):
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"participants": [], "reviewers": []}
    mock_resp.raise_for_status = MagicMock()
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0",
        "tok",
        participant_user_slug="u1",
    )
    assert p.get_bot_blocking_state("PROJ", "repo", 5) == "NOT_BLOCKING"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_bot_blocking_state_needs_work(mock_client):
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "reviewers": [
            {"user": {"slug": "u1"}, "status": "NEEDS_WORK"},
        ]
    }
    mock_resp.raise_for_status = MagicMock()
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0",
        "tok",
        participant_user_slug="u1",
    )
    assert p.get_bot_blocking_state("PROJ", "repo", 5) == "BLOCKING"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_bot_blocking_state_participants_needs_work(mock_client):
    """Participant PUT state is reflected on ``participants``; may be absent from ``reviewers``."""
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "participants": [
            {"user": {"slug": "u1"}, "status": "NEEDS_WORK"},
        ],
        "reviewers": [],
    }
    mock_resp.raise_for_status = MagicMock()
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0",
        "tok",
        participant_user_slug="u1",
    )
    assert p.get_bot_blocking_state("PROJ", "repo", 5) == "BLOCKING"


@patch("code_review.providers.bitbucket_server.httpx.Client")
def test_get_bot_blocking_state_participants_precedence_over_reviewers(mock_client):
    """``participants`` wins when both lists include the user (participant PUT semantics)."""
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "participants": [
            {"user": {"slug": "u1"}, "status": "APPROVED"},
        ],
        "reviewers": [
            {"user": {"slug": "u1"}, "status": "NEEDS_WORK"},
        ],
    }
    mock_resp.raise_for_status = MagicMock()
    mock_client.return_value.__enter__.return_value.get.return_value = mock_resp

    p = BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0",
        "tok",
        participant_user_slug="u1",
    )
    assert p.get_bot_blocking_state("PROJ", "repo", 5) == "NOT_BLOCKING"
