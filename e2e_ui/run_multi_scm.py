"""Standalone script: multi-SCM Jenkins flow (one folder + wrapper job per SCM).

Run from repo root: python -m e2e_ui.run_multi_scm
All configuration via .env; see docs/E2E-UI-JENKINS.md and .env.example for required variables.
"""

from __future__ import annotations

from e2e_ui.core.runner import (
    GITEA_WEBHOOK_PARAMS,
    ensure_playwright,
    get_credentials,
    get_jenkins_config,
    get_repo_and_branch,
    jenkins_session,
)


def main() -> None:
    ensure_playwright()
    base_url, username, password = get_jenkins_config()
    creds = get_credentials()
    repo_url, branch = get_repo_and_branch()

    with jenkins_session(base_url, username, password) as ui:
        # Folder + job for Gitea
        ui.create_folder("code-review-gitea")
        for cid, secret in creds.items():
            ui.add_credential_in_folder("code-review-gitea", cid, secret)
        ui.create_pipeline_job(
            name="code-review",
            script_path="docker/jenkins/Jenkinsfile",
            repo_url=repo_url,
            branch=branch,
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
            script_path="docker/jenkins/Jenkinsfile",
            repo_url=repo_url,
            branch=branch,
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

    print("Multi-SCM flow completed.")


if __name__ == "__main__":
    main()
