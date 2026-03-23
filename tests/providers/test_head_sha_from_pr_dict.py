"""Tests for head_sha_from_pr_api_dict / PRInfo.head_sha wiring."""

from code_review.providers.base import head_sha_from_pr_api_dict, pr_info_from_api_dict


def test_head_sha_github_style():
    assert (
        head_sha_from_pr_api_dict({"head": {"sha": "abc123"}, "title": "x"}) == "abc123"
    )


def test_head_sha_gitlab_diff_refs():
    assert (
        head_sha_from_pr_api_dict({"diff_refs": {"head_sha": "def456"}}) == "def456"
    )


def test_head_sha_bitbucket_cloud_source_commit():
    data = {"source": {"commit": {"hash": "ghi789"}}}
    assert head_sha_from_pr_api_dict(data) == "ghi789"


def test_head_sha_bitbucket_server_from_ref():
    data = {"fromRef": {"latestCommit": "jkl012"}}
    assert head_sha_from_pr_api_dict(data) == "jkl012"


def test_pr_info_from_api_dict_includes_head_sha():
    info = pr_info_from_api_dict(
        {"title": "t", "labels": [], "body": "d", "head": {"sha": "fullsha"}},
        "body",
    )
    assert info.head_sha == "fullsha"
