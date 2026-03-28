from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "bitbucket_pull_request_api.py"
AUTH_SECRET_FIELD = "pass" + "word"
TEST_USERNAME = "alice"
TEST_AUTH_SECRET = "fixture-auth-token"


def load_module():
    spec = importlib.util.spec_from_file_location("bitbucket_pull_request_api", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_delete_pull_request_reads_current_version_before_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    requests: list[tuple[str, str, dict[str, int]]] = []

    def fake_get_pull_request(*args, **kwargs):
        return {"id": 17, "title": "feature/test -> main", "version": 4}

    def fake_bitbucket_request(method, url, *, username, payload=None, **kwargs):
        requests.append((method, url, payload or {}))
        assert username == TEST_USERNAME
        assert kwargs[AUTH_SECRET_FIELD] == TEST_AUTH_SECRET
        return None

    monkeypatch.setattr(module, "get_pull_request", fake_get_pull_request)
    monkeypatch.setattr(module, "bitbucket_request", fake_bitbucket_request)

    credentials = {"username": TEST_USERNAME, AUTH_SECRET_FIELD: TEST_AUTH_SECRET}
    result = module.delete_pull_request(
        "PRJ",
        "demo-repo",
        17,
        **credentials,
    )

    assert result["id"] == 17
    assert requests == [
        ("DELETE", module.pull_requests_url("PRJ", "demo-repo", 17), {"version": 4})
    ]


def test_pull_request_version_requires_integer_value() -> None:
    module = load_module()

    with pytest.raises(RuntimeError, match="valid integer 'version'"):
        module.pull_request_version({"id": 17, "version": "not-an-int"})


def test_list_pull_request_comments_reads_all_activity_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    requests: list[tuple[str, str, dict[str, int]]] = []
    responses = iter(
        [
            {
                "values": [
                    {"action": "COMMENTED", "comment": {"id": 10, "text": "first"}},
                    {"action": "UPDATED", "comment": {"id": 999, "text": "ignore"}},
                ],
                "isLastPage": False,
                "nextPageStart": 100,
            },
            {
                "values": [
                    {"action": "COMMENTED", "comment": {"id": 11, "text": "second"}},
                ],
                "isLastPage": True,
            },
        ]
    )

    def fake_bitbucket_request(method, url, *, username, payload=None, params=None, **kwargs):
        requests.append((method, url, params or {}))
        assert username == TEST_USERNAME
        assert kwargs[AUTH_SECRET_FIELD] == TEST_AUTH_SECRET
        return next(responses)

    monkeypatch.setattr(module, "bitbucket_request", fake_bitbucket_request)

    credentials = {"username": TEST_USERNAME, AUTH_SECRET_FIELD: TEST_AUTH_SECRET}
    result = module.list_pull_request_comments(
        "PRJ",
        "demo-repo",
        17,
        **credentials,
    )

    assert result == [{"id": 10, "text": "first"}, {"id": 11, "text": "second"}]
    assert requests == [
        (
            "GET",
            module.pull_request_comments_url("PRJ", "demo-repo", 17, activities=True),
            {"start": 0, "limit": 100},
        ),
        (
            "GET",
            module.pull_request_comments_url("PRJ", "demo-repo", 17, activities=True),
            {"start": 100, "limit": 100},
        ),
    ]


def test_list_pull_request_comments_merges_activity_comment_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()

    def fake_bitbucket_request(method, url, *, username, payload=None, params=None, **kwargs):
        assert method == "GET"
        assert url == module.pull_request_comments_url("PRJ", "demo-repo", 17, activities=True)
        assert username == TEST_USERNAME
        assert kwargs[AUTH_SECRET_FIELD] == TEST_AUTH_SECRET
        assert params == {"start": 0, "limit": 100}
        return {
            "values": [
                {
                    "action": "COMMENTED",
                    "comment": {"id": 42, "text": "raw comment", "state": "OPEN"},
                    "commentAnchor": {"path": "src/Foo.java", "line": 5, "orphaned": True},
                }
            ],
            "isLastPage": True,
        }

    monkeypatch.setattr(module, "bitbucket_request", fake_bitbucket_request)

    credentials = {"username": TEST_USERNAME, AUTH_SECRET_FIELD: TEST_AUTH_SECRET}
    result = module.list_pull_request_comments(
        "PRJ",
        "demo-repo",
        17,
        **credentials,
    )

    assert result == [
        {
            "id": 42,
            "text": "raw comment",
            "state": "OPEN",
            "anchor": {"path": "src/Foo.java", "line": 5, "orphaned": True},
        }
    ]


def test_list_pull_request_comments_raises_on_pagination_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    requests: list[tuple[str, str, dict[str, int]]] = []
    responses = iter(
        [
            {
                "values": [
                    {"action": "COMMENTED", "comment": {"id": 10, "text": "first"}},
                ],
                "isLastPage": False,
                "nextPageStart": 100,
            },
            {
                "values": [
                    {"action": "COMMENTED", "comment": {"id": 11, "text": "second"}},
                ],
                "isLastPage": False,
                "nextPageStart": 0,
            },
        ]
    )

    def fake_bitbucket_request(method, url, *, username, payload=None, params=None, **kwargs):
        requests.append((method, url, params or {}))
        assert username == TEST_USERNAME
        assert kwargs[AUTH_SECRET_FIELD] == TEST_AUTH_SECRET
        return next(responses)

    monkeypatch.setattr(module, "bitbucket_request", fake_bitbucket_request)

    credentials = {"username": TEST_USERNAME, AUTH_SECRET_FIELD: TEST_AUTH_SECRET}
    with pytest.raises(RuntimeError, match="pagination cycle detected"):
        module.list_pull_request_comments(
            "PRJ",
            "demo-repo",
            17,
            **credentials,
        )

    assert requests == [
        (
            "GET",
            module.pull_request_comments_url("PRJ", "demo-repo", 17, activities=True),
            {"start": 0, "limit": 100},
        ),
        (
            "GET",
            module.pull_request_comments_url("PRJ", "demo-repo", 17, activities=True),
            {"start": 100, "limit": 100},
        ),
    ]


def test_list_pull_request_comments_raises_when_max_pages_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    request_count = 0

    def fake_bitbucket_request(method, url, *, username, payload=None, params=None, **kwargs):
        nonlocal request_count
        request_count += 1
        assert method == "GET"
        assert url == module.pull_request_comments_url("PRJ", "demo-repo", 17, activities=True)
        assert username == TEST_USERNAME
        assert kwargs[AUTH_SECRET_FIELD] == TEST_AUTH_SECRET
        assert params == {"start": request_count - 1, "limit": 100}
        return {
            "values": [
                {"action": "COMMENTED", "comment": {"id": request_count, "text": f"comment-{request_count}"}},
            ],
            "isLastPage": False,
            "nextPageStart": request_count,
        }

    monkeypatch.setattr(module, "bitbucket_request", fake_bitbucket_request)

    credentials = {"username": TEST_USERNAME, AUTH_SECRET_FIELD: TEST_AUTH_SECRET}
    with pytest.raises(RuntimeError, match="exceeded max_pages=500"):
        module.list_pull_request_comments(
            "PRJ",
            "demo-repo",
            17,
            **credentials,
        )

    assert request_count == 500


def test_delete_pull_request_comment_passes_version_query_param(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    requests: list[tuple[str, str, dict[str, int]]] = []

    def fake_get_pull_request_comment(*args, **kwargs):
        return {"id": 42, "version": 7, "text": "cleanup"}

    def fake_bitbucket_request(method, url, *, username, payload=None, params=None, **kwargs):
        requests.append((method, url, params or {}))
        assert username == TEST_USERNAME
        assert kwargs[AUTH_SECRET_FIELD] == TEST_AUTH_SECRET
        return None

    monkeypatch.setattr(module, "get_pull_request_comment", fake_get_pull_request_comment)
    monkeypatch.setattr(module, "bitbucket_request", fake_bitbucket_request)

    credentials = {"username": TEST_USERNAME, AUTH_SECRET_FIELD: TEST_AUTH_SECRET}
    result = module.delete_pull_request_comment(
        "PRJ",
        "demo-repo",
        17,
        42,
        **credentials,
    )

    assert result["id"] == 42
    assert requests == [
        (
            "DELETE",
            module.pull_request_comments_url("PRJ", "demo-repo", 17, 42),
            {"version": 7},
        )
    ]


def test_list_pull_request_tasks_reads_all_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    requests: list[tuple[str, str, dict[str, int]]] = []
    responses = iter(
        [
            {
                "values": [{"id": 9, "state": "OPEN", "text": "first task"}],
                "isLastPage": False,
                "nextPageStart": 100,
            },
            {
                "values": [{"id": 10, "state": "RESOLVED", "text": "second task"}],
                "isLastPage": True,
            },
        ]
    )

    def fake_bitbucket_request(method, url, *, username, payload=None, params=None, **kwargs):
        requests.append((method, url, params or {}))
        assert username == TEST_USERNAME
        assert kwargs[AUTH_SECRET_FIELD] == TEST_AUTH_SECRET
        return next(responses)

    monkeypatch.setattr(module, "bitbucket_request", fake_bitbucket_request)

    credentials = {"username": TEST_USERNAME, AUTH_SECRET_FIELD: TEST_AUTH_SECRET}
    result = module.list_pull_request_tasks(
        "PRJ",
        "demo-repo",
        17,
        **credentials,
    )

    assert result == [
        {"id": 9, "state": "OPEN", "text": "first task"},
        {"id": 10, "state": "RESOLVED", "text": "second task"},
    ]
    assert requests == [
        (
            "GET",
            module.pull_request_tasks_url("PRJ", "demo-repo", 17),
            {"start": 0, "limit": 100},
        ),
        (
            "GET",
            module.pull_request_tasks_url("PRJ", "demo-repo", 17),
            {"start": 100, "limit": 100},
        ),
    ]


def test_list_pull_request_activities_reads_all_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    requests: list[tuple[str, str, dict[str, int]]] = []
    responses = iter(
        [
            {
                "values": [{"id": 1, "action": "COMMENTED"}],
                "isLastPage": False,
                "nextPageStart": 100,
            },
            {
                "values": [{"id": 2, "action": "APPROVED"}],
                "isLastPage": True,
            },
        ]
    )

    def fake_bitbucket_request(method, url, *, username, payload=None, params=None, **kwargs):
        requests.append((method, url, params or {}))
        assert username == TEST_USERNAME
        assert kwargs[AUTH_SECRET_FIELD] == TEST_AUTH_SECRET
        return next(responses)

    monkeypatch.setattr(module, "bitbucket_request", fake_bitbucket_request)

    credentials = {"username": TEST_USERNAME, AUTH_SECRET_FIELD: TEST_AUTH_SECRET}
    result = module.list_pull_request_activities(
        "PRJ",
        "demo-repo",
        17,
        **credentials,
    )

    assert result == [{"id": 1, "action": "COMMENTED"}, {"id": 2, "action": "APPROVED"}]
    assert requests == [
        (
            "GET",
            module.pull_request_comments_url("PRJ", "demo-repo", 17, activities=True),
            {"start": 0, "limit": 100},
        ),
        (
            "GET",
            module.pull_request_comments_url("PRJ", "demo-repo", 17, activities=True),
            {"start": 100, "limit": 100},
        ),
    ]
