from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "bitbucket_pull_request_comments.py"


def load_module():
    spec = importlib.util.spec_from_file_location("bitbucket_pull_request_comments", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_main_lists_pull_request_comments_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_module()

    monkeypatch.setattr(module, "load_script_credentials", lambda: ("alice", "secret"))
    monkeypatch.setattr(
        module,
        "list_pull_request_comments",
        lambda *args, **kwargs: [{"id": 10, "text": "first"}],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bitbucket_pull_request_comments.py",
            "list",
            "PRJ",
            "demo-repo",
            "17",
        ],
    )

    assert module.main() == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == '[\n  {\n    "id": 10,\n    "text": "first"\n  }\n]'


def test_main_deletes_pull_request_comment_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_module()

    monkeypatch.setattr(module, "load_script_credentials", lambda: ("alice", "secret"))
    monkeypatch.setattr(
        module,
        "delete_pull_request_comment",
        lambda *args, **kwargs: {"id": 42, "text": "cleanup"},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bitbucket_pull_request_comments.py",
            "delete",
            "PRJ",
            "demo-repo",
            "17",
            "42",
        ],
    )

    assert module.main() == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "Deleted comment #42 from pull request #17"
