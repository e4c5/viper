from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "bitbucket_pull_request_api.py"


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

    def fake_bitbucket_request(method, url, *, username, password, payload=None):
        requests.append((method, url, payload or {}))
        assert username == "alice"
        assert password == "secret"
        return None

    monkeypatch.setattr(module, "get_pull_request", fake_get_pull_request)
    monkeypatch.setattr(module, "bitbucket_request", fake_bitbucket_request)

    result = module.delete_pull_request(
        "PRJ",
        "demo-repo",
        17,
        username="alice",
        password="secret",
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

    def fake_bitbucket_request(method, url, *, username, password, payload=None, params=None):
        requests.append((method, url, params or {}))
        return next(responses)

    monkeypatch.setattr(module, "bitbucket_request", fake_bitbucket_request)

    result = module.list_pull_request_comments(
        "PRJ",
        "demo-repo",
        17,
        username="alice",
        password="secret",
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


def test_delete_pull_request_comment_passes_version_query_param(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    requests: list[tuple[str, str, dict[str, int]]] = []

    def fake_get_pull_request_comment(*args, **kwargs):
        return {"id": 42, "version": 7, "text": "cleanup"}

    def fake_bitbucket_request(method, url, *, username, password, payload=None, params=None):
        requests.append((method, url, params or {}))
        return None

    monkeypatch.setattr(module, "get_pull_request_comment", fake_get_pull_request_comment)
    monkeypatch.setattr(module, "bitbucket_request", fake_bitbucket_request)

    result = module.delete_pull_request_comment(
        "PRJ",
        "demo-repo",
        17,
        42,
        username="alice",
        password="secret",
    )

    assert result["id"] == 42
    assert requests == [
        (
            "DELETE",
            module.pull_request_comments_url("PRJ", "demo-repo", 17, 42),
            {"version": 7},
        )
    ]
