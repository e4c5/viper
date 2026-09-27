"""Provider edge-case tests: malformed payloads, 404s, pagination termination.

All HTTP seams are mocked (``_get``/``_post``/``_patch``/``_put``/``_delete``
and the paginated helpers); no real network.
"""

from unittest.mock import MagicMock

import httpx
import pytest

from code_review.providers.base import BotAttributionIdentity
from code_review.providers.bitbucket_server import BitbucketServerProvider
from code_review.providers.gitea import GiteaProvider
from code_review.providers.gitlab import GitLabProvider


def _http_error(status: int) -> httpx.HTTPStatusError:
    resp = MagicMock()
    resp.status_code = status
    return httpx.HTTPStatusError(f"HTTP {status}", request=MagicMock(), response=resp)


# ---------------------------------------------------------------------------
# Gitea
# ---------------------------------------------------------------------------


def test_gitea_get_existing_review_comments_404_returns_empty():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(side_effect=_http_error(404))
    assert p.get_existing_review_comments("o", "r", 1) == []


def test_gitea_get_existing_review_comments_500_raises():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(side_effect=_http_error(500))
    with pytest.raises(httpx.HTTPStatusError):
        p.get_existing_review_comments("o", "r", 1)


def test_gitea_get_existing_review_comments_non_list_returns_empty():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(return_value={"unexpected": "dict"})
    assert p.get_existing_review_comments("o", "r", 1) == []


def test_gitea_get_existing_review_comments_parses_fields():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(
        return_value=[
            {"id": 5, "path": "a.py", "line": "12", "body": "b", "resolved": True},
            {"id": 6, "body": "no path"},
        ]
    )
    out = p.get_existing_review_comments("o", "r", 1)
    assert len(out) == 2
    assert out[0].id == "5"
    assert out[0].line == 12
    assert out[0].resolved is True
    assert out[1].path == ""


def test_gitea_token_user_login_lower_success_and_failure():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(return_value={"login": "BotUser"})
    assert p._gitea_token_user_login_lower() == "botuser"
    p._get = MagicMock(return_value=["not-a-dict"])
    assert p._gitea_token_user_login_lower() is None
    p._get = MagicMock(side_effect=RuntimeError("net"))
    assert p._gitea_token_user_login_lower() is None


def test_gitea_list_pull_reviews_paginates_until_short_page():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(
        side_effect=[
            [{"id": i} for i in range(100)],
            [{"id": 200}],
        ]
    )
    out = p._gitea_list_pull_reviews("o", "r", 1)
    assert len(out) == 101
    assert p._get.call_count == 2
    assert p._get.call_args_list[0][1]["params"]["page"] == 1
    assert p._get.call_args_list[1][1]["params"]["page"] == 2


def test_gitea_list_pull_reviews_stops_on_non_list():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(return_value={"not": "a list"})
    assert p._gitea_list_pull_reviews("o", "r", 1) == []


@pytest.mark.parametrize("code", [404, 405, 501])
def test_gitea_list_pull_reviews_unsupported_endpoint_returns_none(code):
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(side_effect=_http_error(code))
    assert p._gitea_list_pull_reviews("o", "r", 1) is None


def test_gitea_list_pull_reviews_http_500_and_generic_error_return_none():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(side_effect=_http_error(500))
    assert p._gitea_list_pull_reviews("o", "r", 1) is None
    p._get = MagicMock(side_effect=RuntimeError("boom"))
    assert p._gitea_list_pull_reviews("o", "r", 1) is None


def test_gitea_get_bot_attribution_identity_success_and_failure():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(return_value={"login": "MyBot", "id": 42})
    ident = p.get_bot_attribution_identity("o", "r", 1)
    assert ident.login == "mybot"
    assert ident.id_str == "42"
    p._get = MagicMock(side_effect=RuntimeError("net"))
    assert p.get_bot_attribution_identity("o", "r", 1) == BotAttributionIdentity()


def test_gitea_submit_review_decision_404_warns_and_returns(caplog):
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._post = MagicMock(side_effect=_http_error(404))
    p.submit_review_decision("o", "r", 1, "APPROVE")
    assert "not supported" in caplog.text


def test_gitea_submit_review_decision_other_error_raises():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._post = MagicMock(side_effect=_http_error(403))
    with pytest.raises(httpx.HTTPStatusError):
        p.submit_review_decision("o", "r", 1, "APPROVE")


def test_gitea_resolve_unresolve_swallow_http_errors():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._patch = MagicMock(side_effect=_http_error(405))
    p.resolve_comment("o", "r", "c1")  # must not raise
    p.unresolve_comment("o", "r", "c1")
    assert p._patch.call_count == 2


def test_gitea_get_pr_commit_messages_paginates_and_stops_on_error():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(
        side_effect=[
            [{"message": f"c{i}"} for i in range(50)],
            RuntimeError("net down"),
        ]
    )
    out = p.get_pr_commit_messages("o", "r", 1)
    assert len(out) == 50
    assert out[0] == "c0"


def test_gitea_get_file_content_invalid_base64_raises():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(return_value={"content": "!!!not-base64!!!"})
    with pytest.raises(ValueError, match="Invalid base64"):
        p.get_file_content("o", "r", "main", "f.py")


def test_gitea_get_file_content_unexpected_response_raises():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get = MagicMock(return_value={"no_content": True})
    with pytest.raises(ValueError, match="Unexpected response"):
        p.get_file_content("o", "r", "main", "f.py")


def test_gitea_incremental_diff_non_fallback_status_raises():
    p = GiteaProvider("https://gitea.example.com", "tok")
    p._get_text = MagicMock(side_effect=_http_error(500))
    with pytest.raises(httpx.HTTPStatusError):
        p._get_incremental_pr_diff("o", "r", 1, "base", "head")


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


def _gitlab_provider():
    return GitLabProvider("https://gitlab.example.com/api/v4", "tok")


def test_gitlab_get_pr_files_malformed_entries_skipped():
    p = _gitlab_provider()
    p._get = MagicMock(
        return_value=[
            "not-a-dict",
            {"old_path": "gone.py", "deleted_file": True},
            {"new_file": True},  # no path -> skipped
            {"new_path": "ok.py", "new_lines": 3, "deleted_lines": 1},
            {"new_path": "diffcount.py", "diff": "+a\n+b\n-c\n"},
        ]
    )
    out = p.get_pr_files("o", "r", 1)
    statuses = {f.path: f.status for f in out}
    assert statuses == {
        "gone.py": "removed",
        "ok.py": "modified",
        "diffcount.py": "modified",
    }
    adds = {f.path: (f.additions, f.deletions) for f in out}
    assert adds["ok.py"] == (3, 1)
    assert adds["diffcount.py"] == (2, 1)


def test_gitlab_get_pr_files_non_list_returns_empty():
    p = _gitlab_provider()
    p._get = MagicMock(return_value={"diffs": []})
    assert p.get_pr_files("o", "r", 1) == []


def test_gitlab_bot_blocking_state_no_user_id():
    p = _gitlab_provider()
    p._get = MagicMock(return_value={"no": "id"})
    assert p.get_bot_blocking_state("o", "r", 1) == "UNKNOWN"


def test_gitlab_bot_blocking_state_reviews_404_unknown():
    p = _gitlab_provider()
    p._get = MagicMock(return_value={"id": 7})
    p._get_mr_reviews_paginated = MagicMock(side_effect=_http_error(404))
    assert p.get_bot_blocking_state("o", "r", 1) == "UNKNOWN"


def test_gitlab_bot_blocking_state_generic_error_unknown():
    p = _gitlab_provider()
    p._get = MagicMock(side_effect=RuntimeError("net"))
    assert p.get_bot_blocking_state("o", "r", 1) == "UNKNOWN"


def test_gitlab_bot_blocking_state_latest_review_wins():
    p = _gitlab_provider()
    p._get = MagicMock(return_value={"id": 7})
    p._get_mr_reviews_paginated = MagicMock(
        return_value=[
            {"id": 2, "state": "approved", "user": {"id": 7}},
            {"id": 3, "state": "requested_changes", "user": {"id": 7}},
            {"id": 4, "state": "approved", "user": {"id": 99}},
        ]
    )
    assert p.get_bot_blocking_state("o", "r", 1) == "BLOCKING"

    p._get_mr_reviews_paginated = MagicMock(return_value=[])
    assert p.get_bot_blocking_state("o", "r", 1) == "NOT_BLOCKING"


def test_gitlab_submit_review_decision_approve_with_sha():
    p = _gitlab_provider()
    p._post = MagicMock()
    p.submit_review_decision("o", "r", 1, "APPROVE", head_sha="abc123")
    p._post.assert_called_once()
    path, payload = p._post.call_args[0]
    assert path.endswith("/merge_requests/1/approve")
    assert payload == {"sha": "abc123"}


def test_gitlab_submit_review_decision_request_changes_unapproves_then_notes():
    p = _gitlab_provider()
    p._post = MagicMock()
    p._delete = MagicMock()
    p.submit_review_decision("o", "r", 1, "REQUEST_CHANGES", body="please fix")
    p._delete.assert_called_once()
    assert p._delete.call_args[0][0].endswith("/approve")
    note_path, note_payload = p._post.call_args[0]
    assert note_path.endswith("/notes")
    assert "requested_changes" in note_payload["body"]


def test_gitlab_submit_review_decision_unapprove_404_is_soft():
    p = _gitlab_provider()
    p._post = MagicMock()
    p._delete = MagicMock(side_effect=_http_error(404))
    p.submit_review_decision("o", "r", 1, "REQUEST_CHANGES")  # no raise
    p._post.assert_called_once()


def test_gitlab_get_pr_commit_messages_paginates_and_filters():
    p = _gitlab_provider()
    p._paginate_list = MagicMock(
        return_value=iter(
            [
                [{"message": "m1"}, {"title": "t2"}, {"message": ""}, "junk"],
                [{"message": "m3"}],
            ]
        )
    )
    assert p.get_pr_commit_messages("o", "r", 1) == ["m1", "t2", "m3"]


def test_gitlab_get_pr_commit_messages_error_returns_partial(caplog):
    def gen():
        yield [{"message": "first"}]
        raise RuntimeError("net")

    p = _gitlab_provider()
    p._paginate_list = MagicMock(return_value=gen())
    assert p.get_pr_commit_messages("o", "r", 1) == ["first"]


def test_gitlab_get_pr_info_success_non_dict_and_error():
    p = _gitlab_provider()
    p._get = MagicMock(
        return_value={"title": "T", "description": "D", "sha": "abc"}
    )
    info = p.get_pr_info("o", "r", 1)
    assert info is not None and info.title == "T"

    p._get = MagicMock(return_value=["nope"])
    assert p.get_pr_info("o", "r", 1) is None

    p._get = MagicMock(side_effect=RuntimeError("net"))
    assert p.get_pr_info("o", "r", 1) is None


def test_gitlab_update_pr_description_with_and_without_title():
    p = _gitlab_provider()
    p._put = MagicMock()
    p.update_pr_description("o", "r", 1, "body")
    p._put.assert_called_with(
        p._path("o", "r", "merge_requests", "1"), {"description": "body"}
    )
    p.update_pr_description("o", "r", 1, "body", title="T")
    p._put.assert_called_with(
        p._path("o", "r", "merge_requests", "1"),
        {"description": "body", "title": "T"},
    )


def test_gitlab_bot_attribution_identity_success_and_failure():
    p = _gitlab_provider()
    p._get = MagicMock(return_value={"username": "GLBot", "id": 9})
    ident = p.get_bot_attribution_identity("o", "r", 1)
    assert ident.login == "glbot"
    assert ident.id_str == "9"
    p._get = MagicMock(side_effect=RuntimeError("x"))
    assert p.get_bot_attribution_identity("o", "r", 1) == BotAttributionIdentity()


def test_gitlab_resolve_review_thread_direct_id_puts_resolved():
    p = _gitlab_provider()
    p._put = MagicMock()
    ctx = MagicMock(thread_id="disc-9")
    p.resolve_review_thread("o", "r", 1, ctx, "c1")
    path, payload = p._put.call_args[0]
    assert "discussions/disc-9" in path
    assert payload == {"resolved": True}


def test_gitlab_resolve_review_thread_no_id_raises():
    p = _gitlab_provider()
    ctx = MagicMock(thread_id="")
    p.get_review_thread_dismissal_context = MagicMock(return_value=None)
    with pytest.raises(ValueError, match="discussion id is required"):
        p.resolve_review_thread("o", "r", 1, ctx, "c1")


def test_gitlab_post_review_thread_reply_no_discussion_raises():
    p = _gitlab_provider()
    p._get_mr_discussions_paginated = MagicMock(return_value=[])
    with pytest.raises(ValueError, match="No GitLab discussion"):
        p.post_review_thread_reply("o", "r", 1, "77", "reply")


def test_gitlab_post_review_thread_reply_posts_to_discussion():
    p = _gitlab_provider()
    p._get_mr_discussions_paginated = MagicMock(
        return_value=[{"id": "d1", "notes": [{"id": 77, "body": "x"}]}]
    )
    p._post = MagicMock()
    p.post_review_thread_reply("o", "r", 1, "77", "reply text")
    path, payload = p._post.call_args[0]
    assert "discussions/d1/notes" in path
    assert payload == {"body": "reply text"}


def test_gitlab_discussion_lookup_empty_id_and_error():
    p = _gitlab_provider()
    assert p._gitlab_discussion_id_for_note_id("o", "r", 1, "  ") == ""
    p._get_mr_discussions_paginated = MagicMock(side_effect=RuntimeError("x"))
    assert p._gitlab_discussion_id_for_note_id("o", "r", 1, "5") == ""


def test_gitlab_unresolved_items_skips_resolved_and_no_diff_notes():
    p = _gitlab_provider()
    p._get_mr_discussions_paginated = MagicMock(
        return_value=[
            {"id": "d-resolved", "resolved": True, "notes": []},
            {"id": "d-plain", "resolved": False, "notes": [{"type": "Note", "body": "x"}]},
            {
                "id": "d-diff",
                "resolved": False,
                "notes": [
                    {
                        "type": "DiffNote",
                        "body": "[High] bug",
                        "position": {"new_path": "a.py", "new_line": 9},
                    }
                ],
            },
        ]
    )
    out = p.get_unresolved_review_items_for_quality_gate("o", "r", 1)
    assert len(out) == 1
    assert out[0].thread_id == "d-diff"
    assert out[0].inferred_severity == "high"
    assert out[0].path == "a.py"
    assert out[0].line == 9


# ---------------------------------------------------------------------------
# Bitbucket Server
# ---------------------------------------------------------------------------


def _bbs():
    return BitbucketServerProvider(
        "https://bb:7990/rest/api/1.0", "tok", bot_identity="viper-bot"
    )


def test_bbs_blocking_state_no_identity_unknown():
    p = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    assert p.get_bot_blocking_state("o", "r", 1) == "UNKNOWN"


def test_bbs_blocking_state_fetch_error_and_non_dict():
    p = _bbs()
    p._get = MagicMock(side_effect=RuntimeError("net"))
    assert p.get_bot_blocking_state("o", "r", 1) == "UNKNOWN"
    p._get = MagicMock(return_value=["nope"])
    assert p.get_bot_blocking_state("o", "r", 1) == "UNKNOWN"


def test_bbs_blocking_state_participants_and_fallback_to_reviewers():
    p = _bbs()
    # participant marked NEEDS_WORK -> blocking
    p._get = MagicMock(
        return_value={
            "participants": [
                {"user": {"slug": "viper-bot"}, "status": "NEEDS_WORK"}
            ]
        }
    )
    assert p.get_bot_blocking_state("o", "r", 1) == "BLOCKING"

    # no participants key; reviewers say APPROVED -> not blocking
    p._get = MagicMock(
        return_value={"reviewers": [{"user": {"slug": "viper-bot"}, "approved": True}]}
    )
    state = p.get_bot_blocking_state("o", "r", 1)
    assert state in ("NOT_BLOCKING", "UNKNOWN")

    # neither key present -> UNKNOWN
    p._get = MagicMock(return_value={})
    assert p.get_bot_blocking_state("o", "r", 1) == "UNKNOWN"


def test_bbs_get_pr_info_variants():
    p = _bbs()
    p._get = MagicMock(
        return_value={
            "title": "T",
            "description": "D",
            "fromRef": {"latestCommit": "sha1"},
        }
    )
    info = p.get_pr_info("o", "r", 1)
    assert info is not None
    assert info.title == "T"
    assert info.head_sha == "sha1"

    p._get = MagicMock(return_value="junk")
    assert p.get_pr_info("o", "r", 1) is None

    p._get = MagicMock(side_effect=RuntimeError("x"))
    assert p.get_pr_info("o", "r", 1) is None


def test_bbs_next_commit_page_start_termination():
    assert BitbucketServerProvider._next_commit_page_start({"isLastPage": True}, 0) is None
    assert (
        BitbucketServerProvider._next_commit_page_start(
            {"isLastPage": False, "nextPageStart": None}, 0
        )
        is None
    )
    assert (
        BitbucketServerProvider._next_commit_page_start(
            {"isLastPage": False, "nextPageStart": 0}, 0
        )
        is None
    )
    assert (
        BitbucketServerProvider._next_commit_page_start(
            {"isLastPage": False, "nextPageStart": 25}, 0
        )
        == 25
    )


def test_bbs_safe_get_commit_page_error_and_non_dict():
    p = _bbs()
    p._get = MagicMock(side_effect=RuntimeError("x"))
    assert p._safe_get_commit_page("/path", 0, "o", "r", 1) is None
    p._get = MagicMock(return_value=["not-dict"])
    assert p._safe_get_commit_page("/path", 0, "o", "r", 1) is None


def test_bbs_get_pr_commit_messages_paginates():
    p = _bbs()
    p._get = MagicMock(
        side_effect=[
            {
                "isLastPage": False,
                "nextPageStart": 2,
                "values": [{"message": "a"}, {"message": "b"}],
            },
            {"isLastPage": True, "values": [{"message": "c"}]},
        ]
    )
    assert p.get_pr_commit_messages("o", "r", 1) == ["a", "b", "c"]


def test_bbs_attribution_identity():
    p = _bbs()
    assert p.get_bot_attribution_identity("o", "r", 1).slug == "viper-bot"
    p2 = BitbucketServerProvider("https://bb:7990/rest/api/1.0", "tok")
    assert p2.get_bot_attribution_identity("o", "r", 1) == BotAttributionIdentity()


def test_bbs_participant_put_after_refetch_409_retries_with_new_version():
    p = _bbs()
    p._pull_request_version = MagicMock(return_value=3)
    put = MagicMock(side_effect=[_http_error(409), None])
    p._bbs_participant_put = put
    ok = p._bbs_try_participant_put_after_refetch(
        "o", "r", 1, "/path", {"status": "APPROVED"}, version2=2
    )
    assert ok is True
    assert put.call_count == 2
    # retried with the freshly read version
    assert put.call_args_list[1][0][2] == 3


def test_bbs_participant_put_after_refetch_second_409_unreadable_version():
    p = _bbs()
    p._pull_request_version = MagicMock(return_value=None)
    p._bbs_participant_put = MagicMock(side_effect=_http_error(409))
    with pytest.raises(ValueError, match="could not read pull request version"):
        p._bbs_try_participant_put_after_refetch("o", "r", 1, "/path", {}, version2=2)


def test_bbs_participant_put_after_refetch_non_400_raises():
    p = _bbs()
    p._bbs_participant_put = MagicMock(side_effect=_http_error(403))
    with pytest.raises(httpx.HTTPStatusError):
        p._bbs_try_participant_put_after_refetch("o", "r", 1, "/path", {}, version2=2)


def test_bbs_participant_put_after_refetch_400_returns_false():
    p = _bbs()
    p._bbs_participant_put = MagicMock(side_effect=_http_error(400))
    assert (
        p._bbs_try_participant_put_after_refetch("o", "r", 1, "/path", {}, version2=2)
        is False
    )


def test_bbs_submit_retry_after_409_refetches_version():
    p = _bbs()
    p._pull_request_version = MagicMock(return_value=5)
    p._bbs_participant_put = MagicMock()
    p._bbs_submit_review_decision_retry_after_409(
        "o", "r", 1, "/path", {}, cause=_http_error(409)
    )
    p._bbs_participant_put.assert_called_once_with("/path", {}, 5)


def test_bbs_submit_retry_after_409_unreadable_version_raises():
    p = _bbs()
    p._pull_request_version = MagicMock(return_value=None)
    with pytest.raises(ValueError, match="could not read pull request version"):
        p._bbs_submit_review_decision_retry_after_409(
            "o", "r", 1, "/path", {}, cause=_http_error(409)
        )


def test_gitlab_project_id_url_encodes_nested_namespace():
    """A nested-group owner ("group/sub") must encode the whole project path."""
    from code_review.providers.gitlab import _project_id

    assert _project_id("group", "repo") == "group%2Frepo"
    assert _project_id("group/sub", "repo") == "group%2Fsub%2Frepo"
    p = _gitlab_provider()
    p._get = MagicMock(return_value=[])
    p.get_pr_files("group/sub", "repo", 1)
    url = p._get.call_args.args[0]
    assert "projects/group%2Fsub%2Frepo/" in url
