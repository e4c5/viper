from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "create_bitbucket_pull_request.py"


def load_module():
    spec = importlib.util.spec_from_file_location("create_bitbucket_pull_request", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_branch_ref_adds_heads_prefix() -> None:
    module = load_module()

    assert module.branch_ref("feature/test") == "refs/heads/feature/test"
    assert module.branch_ref("refs/heads/feature/test") == "refs/heads/feature/test"


def test_build_pr_payload_uses_same_repo_for_source_and_destination() -> None:
    module = load_module()

    payload = module.build_pr_payload("PRJ", "demo-repo", "feature/test", "main")

    assert payload["title"] == "feature/test -> main"
    assert payload["fromRef"]["id"] == "refs/heads/feature/test"
    assert payload["toRef"]["id"] == "refs/heads/main"
    assert payload["fromRef"]["repository"]["slug"] == "demo-repo"
    assert payload["toRef"]["repository"]["slug"] == "demo-repo"
    assert payload["fromRef"]["repository"]["project"]["key"] == "PRJ"
    assert payload["toRef"]["repository"]["project"]["key"] == "PRJ"
