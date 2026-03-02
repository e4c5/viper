"""Pytest configuration and shared fixtures."""

import os
import subprocess

import pytest


E2E_COMPOSE_FILE = "docker-compose.e2e.yml"
E2E_PROJECT_NAME = "code-review-e2e"


@pytest.fixture(scope="session")
def e2e_stack():
    """Bring up the isolated E2E Docker stack for tests marked with @pytest.mark.e2e.

    Uses docker-compose.e2e.yml and a separate project name so normal dev volumes
    are not touched. Requires Docker/Podman to be available.
    """
    # Only attempt to start the stack when E2E tests are explicitly enabled.
    if os.environ.get("RUN_E2E") != "1":
        pytest.skip("E2E stack only started when RUN_E2E=1")

    up_cmd = [
        "docker",
        "compose",
        "-f",
        E2E_COMPOSE_FILE,
        "-p",
        E2E_PROJECT_NAME,
        "up",
        "-d",
    ]
    # Only rebuild images when explicitly requested; keeps normal E2E runs fast.
    if os.environ.get("E2E_REBUILD") == "1":
        up_cmd.append("--build")
    down_cmd = [
        "docker",
        "compose",
        "-f",
        E2E_COMPOSE_FILE,
        "-p",
        E2E_PROJECT_NAME,
        "down",
        "-v",
    ]

    try:
        subprocess.run(up_cmd, check=True)
    except FileNotFoundError:
        pytest.skip("Docker is not available to start E2E stack")
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"Failed to start E2E stack: {exc}")

    try:
        yield
    finally:
        # Always attempt to tear down; ignore errors to avoid masking test results.
        try:
            subprocess.run(down_cmd, check=False)
        except FileNotFoundError:
            pass

