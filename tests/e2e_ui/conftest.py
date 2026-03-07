"""Pytest fixtures for e2e_ui: load .env, browser, and JenkinsUI.

Secrets (SCM_TOKEN, GOOGLE_API_KEY, etc.) are read from .env; variable names
match Jenkins credential IDs. Run with RUN_E2E_UI=1 and ensure Jenkins is up.

Playwright is imported only inside fixtures that use it so CI without the e2e-ui
extra can still collect and run other tests without ModuleNotFoundError.
"""

from __future__ import annotations

import os

import pytest

from tests.e2e_ui.core.env_loader import EnvLoader
from tests.e2e_ui.core.jenkins import JenkinsUI


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "e2e_ui: Playwright UI tests for Jenkins flows (require Jenkins + .env, RUN_E2E_UI=1)",
    )


@pytest.fixture(scope="session")
def e2e_ui_env() -> EnvLoader:
    """Load .env once per session; exposes get_credentials() for Jenkins secrets."""
    return EnvLoader()


@pytest.fixture(scope="session")
def jenkins_base_url() -> str:
    return os.environ.get("JENKINS_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def jenkins_username() -> str:
    username = os.environ.get("JENKINS_USERNAME")
    if not username:
        pytest.skip("JENKINS_USERNAME must be set for e2e_ui tests (e.g. admin for local Jenkins)")
    return username


@pytest.fixture(scope="session")
def jenkins_password() -> str:
    password = os.environ.get("JENKINS_PASSWORD")
    if not password:
        pytest.skip("JENKINS_PASSWORD must be set for e2e_ui tests (e.g. admin for local Jenkins)")
    return password


@pytest.fixture(scope="session")
def playwright_browser():
    """Start Playwright and yield browser; tear down after session."""
    if os.environ.get("RUN_E2E_UI") != "1":
        pytest.skip("e2e_ui tests only run when RUN_E2E_UI=1")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed; install with pip install -e '.[e2e-ui]' and playwright install chromium")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=os.environ.get("E2E_UI_HEADED") != "1")
        yield browser
        browser.close()


@pytest.fixture
def jenkins_page(playwright_browser, jenkins_base_url: str):
    """New page for each test; reuse browser."""
    context = playwright_browser.new_context(
        base_url=jenkins_base_url,
        ignore_https_errors=True,
    )
    page = context.new_page()
    yield page
    context.close()


@pytest.fixture
def jenkins_ui(
    jenkins_page,
    jenkins_base_url: str,
    jenkins_username: str,
    jenkins_password: str,
) -> JenkinsUI:
    """Reusable JenkinsUI instance (logged-in state is per test)."""
    ui = JenkinsUI(
        jenkins_page,
        jenkins_base_url,
        username=jenkins_username,
        password=jenkins_password,
    )
    ui.login()
    return ui
