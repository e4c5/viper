"""Standalone script: multi-SCM Jenkins flow (one folder + wrapper job per SCM).

Run from repo root: python -m e2e_ui.run_multi_scm
Requires: pip install -e ".[e2e-ui]", playwright install chromium, .env with credentials.
Set JENKINS_URL (optional), JENKINS_USERNAME, JENKINS_PASSWORD. Use E2E_UI_HEADED=1 for visible browser.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install -e '.[e2e-ui]' && playwright install chromium", file=sys.stderr)
        sys.exit(1)

    from e2e_ui.core.env_loader import EnvLoader
    from e2e_ui.core.jenkins import JenkinsUI

    base_url = os.environ.get("JENKINS_URL", "http://localhost:8080")
    username = os.environ.get("JENKINS_USERNAME", "").strip()
    password = os.environ.get("JENKINS_PASSWORD", "").strip()
    if not username or not password:
        print("Set JENKINS_USERNAME and JENKINS_PASSWORD (e.g. in .env or export).", file=sys.stderr)
        sys.exit(1)

    env = EnvLoader()
    creds = env.get_credentials()
    if not creds:
        print("No credentials in .env (e.g. SCM_TOKEN, GOOGLE_API_KEY). Add them to run this flow.", file=sys.stderr)
        sys.exit(1)

    GITEA_WEBHOOK_PARAMS = {
        "SCM_OWNER": "$.pull_request.base.repo.owner.login",
        "SCM_REPO": "$.pull_request.base.repo.name",
        "SCM_PR_NUM": "$.pull_request.number",
        "SCM_HEAD_SHA": "$.pull_request.head.sha",
        "PR_ACTION": "$.action",
    }

    repo_url = os.environ.get("E2E_UI_REPO_URL", "https://github.com/your-org/code-review.git")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=os.environ.get("E2E_UI_HEADED") != "1")
        context = browser.new_context(base_url=base_url, ignore_https_errors=True)
        page = context.new_page()
        ui = JenkinsUI(page, base_url, username, password)
        ui.login()

        # Folder + job for Gitea
        ui.create_folder("code-review-gitea")
        for cid, secret in creds.items():
            ui.add_credential_in_folder("code-review-gitea", cid, secret)
        ui.create_pipeline_job(
            name="code-review",
            script_path="docker/jenkins/Jenkinsfile.multi-scm-wrapper",
            repo_url=repo_url,
            branch="main",
            inside_folder="code-review-gitea",
        )
        ui.configure_webhook_trigger(
            job_name="code-review",
            folder_name="code-review-gitea",
            post_content_params=GITEA_WEBHOOK_PARAMS,
            filter_text="$PR_ACTION",
            filter_regex="^(opened|synchronize)$",
        )

        # Folder + job for GitHub
        ui.create_folder("code-review-github")
        for cid, secret in creds.items():
            ui.add_credential_in_folder("code-review-github", cid, secret)
        ui.create_pipeline_job(
            name="code-review",
            script_path="docker/jenkins/Jenkinsfile.multi-scm-wrapper",
            repo_url=repo_url,
            branch="main",
            inside_folder="code-review-github",
        )
        ui.configure_webhook_trigger(
            job_name="code-review",
            folder_name="code-review-github",
            post_content_params=GITEA_WEBHOOK_PARAMS,
            filter_text="$PR_ACTION",
            filter_regex="^(opened|synchronize)$",
        )

        ui.open_job("code-review", folder_name="code-review-gitea")
        ui.open_job("code-review", folder_name="code-review-github")

        context.close()
        browser.close()

    print("Multi-SCM flow completed.")


if __name__ == "__main__":
    main()
