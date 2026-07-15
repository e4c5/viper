from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "simulate_github_webhook.py"
SPEC = importlib.util.spec_from_file_location("simulate_github_webhook", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    "target",
    [
        "file:///etc/passwd",
        "http://user:password@localhost/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/hook",
        "http:///missing-host",
    ],
)
def test_validate_local_target_rejects_unsafe_targets(target):
    with pytest.raises(ValueError):
        MODULE.validate_local_target(target)


def test_validate_local_target_accepts_loopback():
    with patch.object(
        MODULE.socket,
        "getaddrinfo",
        return_value=[(2, 1, 6, "", ("127.0.0.1", 8080))],
    ):
        assert MODULE.validate_local_target("http://localhost:8080/hook") == (
            "http://localhost:8080/hook"
        )


def test_validate_local_target_rejects_mixed_dns_results():
    with patch.object(
        MODULE.socket,
        "getaddrinfo",
        return_value=[
            (2, 1, 6, "", ("127.0.0.1", 8080)),
            (2, 1, 6, "", ("10.0.0.1", 8080)),
        ],
    ):
        with pytest.raises(ValueError, match="loopback"):
            MODULE.validate_local_target("http://localhost:8080/hook")
