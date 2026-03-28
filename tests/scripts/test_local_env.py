from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "local_env.py"


def load_module():
    spec = importlib.util.spec_from_file_location("local_env", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Populate os.environ from the repo-root .env (if present) so that tests can
# consume real credentials without them ever appearing in source code.
load_module().load_local_env()


def test_parse_env_file_preserves_spaces_and_unquotes_values(tmp_path: Path) -> None:
    module = load_module()

    se_password = os.environ.get("SE_PASSWORD")
    if se_password is None:
        pytest.skip("SE_PASSWORD not set in environment – add it to your .env file")

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"SE_PASSWORD={se_password}",
                'LLM_MODEL="gemini-2.5-flash"',
                "export SCM_PROVIDER=bitbucket_server",
            ]
        ),
        encoding="utf-8",
    )

    parsed = module.parse_env_file(env_file)

    assert parsed["SE_PASSWORD"] == se_password
    assert parsed["LLM_MODEL"] == "gemini-2.5-flash"
    assert parsed["SCM_PROVIDER"] == "bitbucket_server"


def test_load_local_env_does_not_override_existing_process_env(tmp_path: Path, monkeypatch) -> None:
    module = load_module()
    env_file = tmp_path / ".env"
    env_file.write_text("SCM_TOKEN=from-file\nLLM_API_KEY=file-key\n", encoding="utf-8")

    monkeypatch.setenv("SCM_TOKEN", "from-process")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    loaded_path = module.load_local_env(env_path=env_file)

    assert loaded_path == env_file
    assert os.environ["SCM_TOKEN"] == "from-process"
    assert os.environ["LLM_API_KEY"] == "file-key"
