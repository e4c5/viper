"""Pytest configuration and shared fixtures."""

import os
import subprocess
import time

import pytest
import requests


def runner_run_async_returning(events):
    """Return a callable that when called returns an async generator yielding events.

    Use for mocking google.adk.runners.Runner.run_async in tests.
    The runner calls run_async(user_id=..., session_id=..., new_message=...);
    the returned callable accepts *args, **kwargs and returns the async generator.
    """

    async def _agen():
        for e in events:
            yield e

    def _wrapper(*args, **kwargs):
        return _agen()

    return _wrapper


E2E_COMPOSE_FILE = "tests/e2e/docker-compose.e2e.yml"
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

    # Wait for Gitea and Jenkins to become ready before yielding to tests.
    timeout_seconds = int(os.environ.get("E2E_READY_TIMEOUT", "120"))
    poll_interval = float(os.environ.get("E2E_READY_POLL_INTERVAL", "2.0"))

    gitea_url = os.environ.get("E2E_GITEA_URL", "http://localhost:3000")
    jenkins_url = os.environ.get("E2E_JENKINS_URL", "http://localhost:8080")

    gitea_health_endpoints = [
        gitea_url,
        f"{gitea_url}/api/v1/version",
    ]
    jenkins_health_endpoints = [
        f"{jenkins_url}/login",
        f"{jenkins_url}/api/json",
    ]

    _wait_for_services_ready(
        gitea_health_endpoints,
        jenkins_health_endpoints,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
    )

    try:
        yield
    finally:
        # Always attempt to tear down; ignore errors to avoid masking test results.
        try:
            subprocess.run(down_cmd, check=False)
        except FileNotFoundError:
            pass


def _service_ready(endpoints):
    for url in endpoints:
        try:
            resp = requests.get(url, timeout=5)
        except requests.RequestException:
            continue
        if 200 <= resp.status_code < 500:
            return True
    return False


def _wait_for_services_ready(
    gitea_endpoints,
    jenkins_endpoints,
    *,
    timeout_seconds: int,
    poll_interval: float,
) -> None:
    start = time.time()
    while True:
        gitea_ready = _service_ready(gitea_endpoints)
        jenkins_ready = _service_ready(jenkins_endpoints)

        if gitea_ready and jenkins_ready:
            return

        if time.time() - start > timeout_seconds:
            pytest.skip(
                f"E2E stack failed to become ready within {timeout_seconds} seconds "
                f"(Gitea ready={gitea_ready}, Jenkins ready={jenkins_ready})"
            )

        time.sleep(poll_interval)
