"""Multi-SCM flow: one folder + wrapper job per SCM, folder credentials, no global SCM env.

Uses core JenkinsUI and secrets from .env; each folder gets SCM_TOKEN and GOOGLE_API_KEY.
"""

import os

import pytest

from tests.e2e_ui.core.jenkins import JenkinsUI

GITEA_WEBHOOK_PARAMS = {
    "SCM_OWNER": "$.pull_request.base.repo.owner.login",
    "SCM_REPO": "$.pull_request.base.repo.name",
    "SCM_PR_NUM": "$.pull_request.number",
    "SCM_HEAD_SHA": "$.pull_request.head.sha",
    "PR_ACTION": "$.action",
}


@pytest.mark.e2e_ui
def test_multi_scm_flow(
    jenkins_ui: JenkinsUI,
    e2e_ui_env,
) -> None:
    """Configure Jenkins for two SCMs: two folders, two jobs (wrapper script path), folder creds."""
    creds = e2e_ui_env.get_credentials()
    if not creds:
        pytest.skip("No credentials in .env for e2e_ui")

    # Folder + job for Gitea
    jenkins_ui.create_folder("code-review-gitea")
    for cid, secret in creds.items():
        jenkins_ui.add_credential_in_folder("code-review-gitea", cid, secret)
    repo_url = os.environ.get("E2E_UI_REPO_URL", "https://github.com/your-org/code-review.git")
    jenkins_ui.create_pipeline_job(
        name="code-review",
        script_path="docker/jenkins/Jenkinsfile.wrapper-gitea",
        repo_url=repo_url,
        branch="main",
        inside_folder="code-review-gitea",
    )
    jenkins_ui.configure_webhook_trigger(
        job_name="code-review",
        folder_name="code-review-gitea",
        post_content_params=GITEA_WEBHOOK_PARAMS,
        filter_text="$PR_ACTION",
        filter_regex="^(opened|synchronize)$",
    )

    # Folder + job for GitHub (wrapper sets SCM_PROVIDER/SCM_URL in script)
    jenkins_ui.create_folder("code-review-github")
    for cid, secret in creds.items():
        jenkins_ui.add_credential_in_folder("code-review-github", cid, secret)
    jenkins_ui.create_pipeline_job(
        name="code-review",
        script_path="docker/jenkins/Jenkinsfile.wrapper-github",
        repo_url=repo_url,
        branch="main",
        inside_folder="code-review-github",
    )
    jenkins_ui.configure_webhook_trigger(
        job_name="code-review",
        folder_name="code-review-github",
        post_content_params=GITEA_WEBHOOK_PARAMS,
        filter_text="$PR_ACTION",
        filter_regex="^(opened|synchronize)$",
    )

    jenkins_ui.open_job("code-review", folder_name="code-review-gitea")
    jenkins_ui.open_job("code-review", folder_name="code-review-github")
