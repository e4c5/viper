"""Single-SCM flow: global credentials, global env, one Pipeline job, webhook trigger.

Uses core JenkinsUI and secrets from .env (SCM_TOKEN, GOOGLE_API_KEY).
"""

import os

import pytest

from tests.e2e_ui.core.jenkins import JenkinsUI

# Gitea-style webhook Post content parameters (JSONPath)
GITEA_WEBHOOK_PARAMS = {
    "SCM_OWNER": "$.pull_request.base.repo.owner.login",
    "SCM_REPO": "$.pull_request.base.repo.name",
    "SCM_PR_NUM": "$.pull_request.number",
    "SCM_HEAD_SHA": "$.pull_request.head.sha",
    "PR_ACTION": "$.action",
}


@pytest.mark.e2e_ui
def test_single_scm_flow(
    jenkins_ui: JenkinsUI,
    e2e_ui_env,
    jenkins_base_url: str,
) -> None:
    """Configure Jenkins for single SCM: global creds, global env, one job, webhook."""
    creds = e2e_ui_env.get_credentials()
    if not creds:
        pytest.skip("No credentials in .env (SCM_TOKEN, GOOGLE_API_KEY) for e2e_ui")

    # Global credentials (same IDs as in docs)
    for cid, secret in creds.items():
        jenkins_ui.add_credential_global(cid, secret)

    # Global env (single SCM)
    jenkins_ui.set_global_env_vars({
        "SCM_PROVIDER": "gitea",
        "SCM_URL": "http://gitea:3000",
    })

    # One pipeline job using main Jenkinsfile (expects global env)
    repo_url = os.environ.get("E2E_UI_REPO_URL", "https://github.com/your-org/code-review.git")
    jenkins_ui.create_pipeline_job(
        name="code-review",
        script_path="docker/jenkins/Jenkinsfile",
        repo_url=repo_url,
        branch="main",
    )

    # Generic Webhook Trigger for Gitea-style payloads
    jenkins_ui.configure_webhook_trigger(
        job_name="code-review",
        post_content_params=GITEA_WEBHOOK_PARAMS,
        filter_text="$PR_ACTION",
        filter_regex="^(opened|synchronize)$",
    )

    # Sanity: open job page
    jenkins_ui.open_job("code-review")
