"""Core helpers for Playwright-based Jenkins UI flows.

- EnvLoader: load secrets from .env (same variable names as Jenkins credential IDs).
- JenkinsUI: reusable Jenkins UI actions (login, folders, credentials, jobs, env, webhooks).
- Target Jenkins version: 2.552 (JENKINS_VERSION_TARGET).
"""

from tests.e2e_ui.core.env_loader import EnvLoader
from tests.e2e_ui.core.jenkins import JENKINS_VERSION_TARGET, JenkinsUI

__all__ = ["EnvLoader", "JenkinsUI", "JENKINS_VERSION_TARGET"]
