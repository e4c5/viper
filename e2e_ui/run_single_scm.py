"""Standalone script: single-SCM Jenkins flow (global creds, global env, one job, webhook).

Run from repo root: python -m e2e_ui.run_single_scm
All configuration via .env; see docs/E2E-UI-JENKINS.md and .env.example for required variables.
"""

from __future__ import annotations

from e2e_ui.core.runner import (
    WEBHOOK_PARAMS_BY_PROVIDER,
    ensure_playwright,
    get_credentials,
    get_jenkins_config,
    get_repo_and_branch,
    jenkins_session,
    require_scm_env,
)


def main() -> None:
    ensure_playwright()
    base_url, username, password = get_jenkins_config()
    creds = get_credentials()
    scm_provider, scm_url = require_scm_env()
    repo_url, branch = get_repo_and_branch()

    with jenkins_session(base_url, username, password) as ui:
        for cid, secret in creds.items():
            ui.add_credential_global(cid, secret)
        ui.set_global_env_vars({
            "SCM_PROVIDER": scm_provider,
            "SCM_URL": scm_url,
        })
        ui.create_pipeline_job(
            name="code-review",
            script_path="docker/jenkins/Jenkinsfile",
            repo_url=repo_url,
            branch=branch,
        )
        webhook_params = WEBHOOK_PARAMS_BY_PROVIDER.get(
            scm_provider, WEBHOOK_PARAMS_BY_PROVIDER["gitea"]
        )
        filter_regex = (
            "^pr:(opened|modified|from_ref_updated)$"
            if scm_provider == "bitbucket_server"
            else "^(opened|synchronize|synchronized)$"
        )
        ui.configure_webhook_trigger(
            job_name="code-review",
            post_content_params=webhook_params,
            filter_text="$PR_ACTION",
            filter_regex=filter_regex,
        )
        ui.open_job("code-review")

    print("Single-SCM flow completed.")


if __name__ == "__main__":
    main()
