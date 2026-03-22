"""Shared setup and session for Playwright Jenkins scripts.

All run_*.py scripts use this module for env loading, validation, and the
Playwright + JenkinsUI session. Flow-specific logic stays in each script.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Generator
from contextlib import contextmanager

# Webhook Post content parameters (JSONPath) per SCM. Used by single- and multi-SCM flows.
GITEA_WEBHOOK_PARAMS = {
    "SCM_OWNER": "$.pull_request.base.repo.owner.login",
    "SCM_REPO": "$.pull_request.base.repo.name",
    "SCM_PR_NUM": "$.pull_request.number",
    "SCM_HEAD_SHA": "$.pull_request.head.sha",
    "PR_ACTION": "$.action",
}
GITHUB_WEBHOOK_PARAMS = {
    "SCM_OWNER": "$.pull_request.base.repo.owner.login",
    "SCM_REPO": "$.pull_request.base.repo.name",
    "SCM_PR_NUM": "$.pull_request.number",
    "SCM_HEAD_SHA": "$.pull_request.head.sha",
    "PR_ACTION": "$.action",
}
GITLAB_WEBHOOK_PARAMS = {
    "SCM_OWNER": "$.project.namespace.path",
    "SCM_REPO": "$.project.path",
    "SCM_PR_NUM": "$.object_attributes.iid",
    "SCM_HEAD_SHA": "$.object_attributes.last_commit.id",
    "PR_ACTION": "$.object_attributes.action",
}
BITBUCKET_WEBHOOK_PARAMS = {
    "SCM_OWNER": "$.pullRequest.toRef.repository.project.key",
    "SCM_REPO": "$.pullRequest.toRef.repository.slug",
    "SCM_PR_NUM": "$.pullRequest.id",
    "SCM_HEAD_SHA": "$.pullRequest.fromRef.latestCommit",
    "PR_ACTION": "$.eventKey",
}
WEBHOOK_PARAMS_BY_PROVIDER = {
    "gitea": GITEA_WEBHOOK_PARAMS,
    "github": GITHUB_WEBHOOK_PARAMS,
    "gitlab": GITLAB_WEBHOOK_PARAMS,
    "bitbucket_server": BITBUCKET_WEBHOOK_PARAMS,
}

SUPPORTED_SCM_PROVIDERS = {"gitea", "github", "gitlab", "bitbucket_server"}


def ensure_playwright() -> None:
    """Ensure Playwright is installed; exit with message if not."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print(
            "Playwright not installed. Run: pip install -e '.[e2e-ui]' && playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)


def get_jenkins_config() -> tuple[str, str, str]:
    """Return (base_url, username, password). Exit if username or password missing."""
    from e2e_ui.core.env_loader import EnvLoader

    EnvLoader()  # load .env
    base_url = os.environ.get("JENKINS_URL", "").strip() or "http://localhost:8080"
    username = os.environ.get("JENKINS_USERNAME", "").strip()
    password = os.environ.get("JENKINS_PASSWORD", "").strip()
    if not username or not password:
        print(
            "Set JENKINS_USERNAME and JENKINS_PASSWORD (e.g. in .env or export).",
            file=sys.stderr,
        )
        sys.exit(1)
    return base_url, username, password


def get_credentials() -> dict[str, str]:
    """Load credentials from .env (same names as Jenkins credential IDs). Exit if none set."""
    from e2e_ui.core.env_loader import EnvLoader

    env = EnvLoader()
    creds = env.get_credentials()
    if not creds:
        print(
            "No credentials in .env (e.g. SCM_TOKEN, GOOGLE_API_KEY). Add them to run this flow.",
            file=sys.stderr,
        )
        sys.exit(1)
    return creds


def get_repo_and_branch() -> tuple[str, str]:
    """Return (repo_url, branch). Exit if E2E_UI_REPO_URL not set."""
    repo_url = os.environ.get("E2E_UI_REPO_URL", "").strip()
    if not repo_url:
        print(
            "Set E2E_UI_REPO_URL in .env to the repo URL for 'Pipeline script from SCM' (e.g. your fork).",
            file=sys.stderr,
        )
        sys.exit(1)
    branch = os.environ.get("E2E_UI_BRANCH", "").strip() or "main"
    return repo_url, branch


def require_scm_env() -> tuple[str, str]:
    """Return (SCM_PROVIDER, SCM_URL). Exit if either not set. Use for single-SCM flow only."""
    scm_provider_raw = os.environ.get("SCM_PROVIDER", "").strip()
    scm_provider = scm_provider_raw.lower()
    scm_url = os.environ.get("SCM_URL", "").strip()
    if not scm_provider_raw or not scm_url:
        print(
            "Single-SCM script requires SCM_PROVIDER and SCM_URL in .env (e.g. SCM_PROVIDER=gitea, SCM_URL=https://gitea.example.com).",
            file=sys.stderr,
        )
        sys.exit(1)
    if scm_provider not in SUPPORTED_SCM_PROVIDERS:
        print(
            f"Invalid SCM_PROVIDER '{scm_provider_raw}'. Supported providers: {', '.join(sorted(SUPPORTED_SCM_PROVIDERS))}.",
            file=sys.stderr,
        )
        sys.exit(1)
    return scm_provider, scm_url


@contextmanager
def jenkins_session(
    base_url: str,
    username: str,
    password: str,
) -> Generator[JenkinsUI, None, None]:
    """Start Playwright, create a logged-in JenkinsUI, yield it, then tear down."""
    from playwright.sync_api import sync_playwright

    from e2e_ui.core.jenkins import JenkinsUI

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=os.environ.get("E2E_UI_HEADED") != "1")
        context = browser.new_context(base_url=base_url, ignore_https_errors=True)
        page = context.new_page()
        ui = JenkinsUI(page, base_url, username, password)
        ui.login()
        try:
            yield ui
        finally:
            context.close()
            browser.close()
