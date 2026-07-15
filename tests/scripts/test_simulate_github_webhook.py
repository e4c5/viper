from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "simulate_github_webhook.py"
SPEC = importlib.util.spec_from_file_location("simulate_github_webhook", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_local_target_rejects_invalid_ports(port):
    with pytest.raises(ValueError, match="between 1 and 65535"):
        MODULE.local_target(port)


def test_local_target_is_fixed_to_loopback_webhook_path():
    assert MODULE.local_target(8080) == "http://localhost:8080/webhooks/github"
