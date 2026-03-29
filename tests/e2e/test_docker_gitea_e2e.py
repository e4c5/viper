"""E2E: bring up isolated Gitea + Jenkins stack, create a hello-world PR via Gitea API,
run the agent (with stubbed LLM), and assert the runner completes successfully.

Usage (from repo root):
  RUN_E2E=1 pytest -m e2e
"""

import base64
import os
import subprocess
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests.conftest import runner_run_async_returning

# Local Docker E2E default; override with E2E_GITEA_ADMIN_PASSWORD.
_E2E_GITEA_DEFAULT_PASSWORD = "e2e-admin-pass"  # NOSONAR S2068 — test fixture only


def _ensure_admin_and_token() -> str:
    """Ensure an admin user exists in the Gitea E2E instance and return a fresh token.

    This uses `docker exec` inside the `code-review-e2e-gitea` container to create an
    admin user (idempotent) and generate a token via the gitea CLI.
    """
    username = os.environ.get("E2E_GITEA_ADMIN_USER", "e2e-admin")
    password = os.environ.get("E2E_GITEA_ADMIN_PASSWORD", _E2E_GITEA_DEFAULT_PASSWORD)
    email = os.environ.get("E2E_GITEA_ADMIN_EMAIL", "e2e-admin@example.com")
    container = os.environ.get("E2E_GITEA_CONTAINER", "code-review-e2e-gitea")

    # Create admin user if it does not already exist. If it exists, this will fail
    # with a non-zero exit code; we ignore that case.
    create_cmd = [
        "docker",
        "exec",
        container,
        "gitea",
        "admin",
        "user",
        "create",
        "--username",
        username,
        "--password",
        password,
        "--email",
        email,
        "--admin",
    ]
    try:
        subprocess.run(create_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        # Assume user already exists; continue
        pass

    # Generate a token for the admin user; --raw prints the token on stdout.
    token_cmd = [
        "docker",
        "exec",
        container,
        "gitea",
        "admin",
        "user",
        "generate-token",
        "--username",
        username,
        "--token-name",
        "e2e-token",
        "--raw",
    ]
    try:
        result = subprocess.run(
            token_cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"E2E could not generate Gitea token via CLI: {exc.stderr}")
    token = (result.stdout or "").strip()
    if not token:
        pytest.skip("E2E generated empty Gitea token")
    return token


def _gitea_client() -> tuple[httpx.Client, str]:
    """Return an httpx client and owner name for the E2E Gitea instance."""
    token = _ensure_admin_and_token()
    base_url = os.environ.get("E2E_GITEA_URL", "http://localhost:3001")
    client = httpx.Client(base_url=base_url, headers={"Authorization": f"token {token}"})
    # Discover the current user to use as owner
    resp = client.get("/api/v1/user")
    resp.raise_for_status()
    owner = resp.json().get("login") or resp.json().get("username")
    if not owner:
        pytest.skip("E2E could not determine Gitea username from /api/v1/user")
    return client, owner


def _ensure_e2e_repo_and_pr() -> tuple[str, str, int, str]:
    """Create (or reuse) a small hello-world repo and PR in Gitea for E2E."""
    client, owner = _gitea_client()
    repo_name = os.environ.get("E2E_REPO_NAME", "code-review-e2e-hello")

    # Create repo if needed
    resp = client.post("/api/v1/user/repos", json={"name": repo_name, "private": False})
    if resp.status_code not in (200, 201, 409):
        pytest.skip(f"E2E failed to create repo: {resp.status_code} {resp.text}")

    # Get repo info to discover default branch
    resp = client.get(f"/api/v1/repos/{owner}/{repo_name}")
    resp.raise_for_status()
    repo_info = resp.json()
    default_branch = repo_info.get("default_branch") or "main"

    # Ensure README on default branch
    content = "# E2E Hello World\n\nThis repository is used for E2E tests.\n"
    encoded = base64.b64encode(content.encode()).decode()
    files_url = f"/api/v1/repos/{owner}/{repo_name}/contents/README.md"
    # Try create, then update if it already exists
    create_payload = {
        "content": encoded,
        "message": "E2E: add README",
        "branch": default_branch,
    }
    resp = client.post(files_url, json=create_payload)
    if resp.status_code == 409:
        # Already exists: update
        # Need the current file SHA; fetch file info
        file_info = client.get(files_url, params={"ref": default_branch}).json()
        update_payload = {
            "content": encoded,
            "message": "E2E: update README",
            "branch": default_branch,
            "sha": file_info.get("sha"),
        }
        client.put(files_url, json=update_payload)

    # Create feature branch from default branch
    feature_branch = "e2e-hello-branch"
    branches_url = f"/api/v1/repos/{owner}/{repo_name}/branches"
    resp = client.post(
        branches_url,
        json={
            "new_branch_name": feature_branch,
            "old_branch_name": default_branch,
        },
    )
    if resp.status_code not in (200, 201, 409):
        pytest.skip(f"E2E failed to create branch: {resp.status_code} {resp.text}")

    # Modify README on feature branch
    updated_content = (
        "# E2E Hello World\n\nThis repository is used for E2E tests.\n\n"
        "Updated by automated E2E test.\n"
    )
    encoded_updated = base64.b64encode(updated_content.encode()).decode()
    file_info = client.get(files_url, params={"ref": feature_branch}).json()
    update_payload = {
        "content": encoded_updated,
        "message": "E2E: update README on feature branch",
        "branch": feature_branch,
        "sha": file_info.get("sha"),
    }
    client.put(files_url, json=update_payload)

    # Create (or reuse) PR
    pulls_url = f"/api/v1/repos/{owner}/{repo_name}/pulls"
    title = "E2E hello-world PR"
    resp = client.post(
        pulls_url,
        json={
            "head": feature_branch,
            "base": default_branch,
            "title": title,
            "body": "Automated PR for E2E tests.",
        },
    )
    if resp.status_code == 409:
        # PR already exists; list open PRs and pick the matching one
        prs = client.get(pulls_url, params={"state": "open"}).json()
        for pr in prs:
            if pr.get("head", {}).get("ref") == feature_branch:
                pr_number = pr.get("number")
                head_sha = pr.get("head", {}).get("sha", "")
                return owner, repo_name, pr_number, head_sha
        pytest.skip("E2E could not find existing PR for feature branch")
    resp.raise_for_status()
    pr = resp.json()
    pr_number = pr.get("number")
    head_sha = pr.get("head", {}).get("sha", "")
    return owner, repo_name, pr_number, head_sha


@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1",
    reason="E2E requires RUN_E2E=1 and Docker Compose with Gitea",
)
def test_e2e_docker_gitea_full_review(e2e_stack):
    """End-to-end: create a hello-world PR in Gitea, run the agent, assert it completes.

    Uses a stubbed LLM so the test does not depend on external LLM latency.
    """
    from code_review.runner import run_review

    owner, repo, pr_number, head_sha = _ensure_e2e_repo_and_pr()

    # Use a mock provider so run_review does not hit any real SCM provider
    # implementation in this E2E test; the Gitea interaction above is exercised
    # directly via httpx.
    mock_provider = MagicMock()
    mock_provider.get_pr_info.return_value = None
    mock_provider.get_existing_review_comments.return_value = []
    mock_file = MagicMock()
    mock_file.path = "foo.py"
    mock_provider.get_pr_files.return_value = [mock_file]
    mock_provider.get_pr_diff.return_value = "diff"
    mock_provider.get_file_content.return_value = "content"

    findings_json = '{"findings":[]}'
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=findings_json)]
    mock_runner_instance = MagicMock()
    mock_runner_instance.run_async = runner_run_async_returning([mock_event])

    with (
        patch("code_review.orchestration_deps.get_provider", return_value=mock_provider),
        patch("google.adk.runners.Runner", return_value=mock_runner_instance),
    ):
        findings = run_review(owner, repo, pr_number, head_sha=head_sha, dry_run=True)

    # Dry run: no posts; we only assert the runner completes and returns a list
    assert isinstance(findings, list)
