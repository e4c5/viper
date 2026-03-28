from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "delete_bitbucket_pull_request.py"


def load_module():
    spec = importlib.util.spec_from_file_location("delete_bitbucket_pull_request", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_main_deletes_pull_request_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_module()

    monkeypatch.setattr(module, "load_script_credentials", lambda: ("alice", "secret"))
    monkeypatch.setattr(
        module,
        "delete_pull_request",
        lambda *args, **kwargs: {"id": 22, "title": "feature/test -> main"},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "delete_bitbucket_pull_request.py",
            "PRJ",
            "demo-repo",
            "22",
        ],
    )

    assert module.main() == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "Deleted pull request #22: feature/test -> main"
