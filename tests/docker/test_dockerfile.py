"""Dockerfile and compose sanity checks (Phase 3)."""

from pathlib import Path

# Repo root (parent of tests/)
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_dockerfile_agent_exists():
    p = REPO_ROOT / "docker" / "Dockerfile.agent"
    assert p.is_file(), "docker/Dockerfile.agent should exist"


def test_dockerfile_agent_content():
    p = REPO_ROOT / "docker" / "Dockerfile.agent"
    content = p.read_text()
    assert "code-review" in content or "code_review" in content
    assert "review" in content


def test_docker_compose_exists():
    p = REPO_ROOT / "docker-compose.yml"
    assert p.is_file(), "docker-compose.yml should exist"


def test_docker_compose_has_gitea_and_jenkins():
    p = REPO_ROOT / "docker-compose.yml"
    content = p.read_text()
    assert "gitea" in content.lower()
    assert "jenkins" in content.lower()


def test_jenkinsfile_exists():
    p = REPO_ROOT / "docker" / "jenkins" / "Jenkinsfile"
    assert p.is_file(), "docker/jenkins/Jenkinsfile should exist"


def test_jenkinsfile_runs_agent():
    # Jenkinsfile is self-contained (no load); contains the full pipeline and runs the agent
    jenkinsfile = REPO_ROOT / "docker" / "jenkins" / "Jenkinsfile"
    assert jenkinsfile.is_file()
    content = jenkinsfile.read_text()
    assert "pipeline {" in content
    assert "REVIEW_IMAGE" in content or "code-review" in content
    assert "docker" in content or "podman" in content or "runtime" in content


def test_jenkinsfile_uses_atlassian_env_names_for_jira_context():
    jenkinsfile = REPO_ROOT / "docker" / "jenkins" / "Jenkinsfile"
    content = jenkinsfile.read_text()

    assert 'export CONTEXT_ATLASSIAN_URL="$CONTEXT_ATLASSIAN_URL"' in content
    assert 'export CONTEXT_ATLASSIAN_EMAIL="$CONTEXT_ATLASSIAN_EMAIL"' in content
    assert 'export CONTEXT_ATLASSIAN_TOKEN="$CONTEXT_ATLASSIAN_TOKEN"' in content
    assert 'RD_ENV="$RD_ENV -e CONTEXT_ATLASSIAN_URL"' in content
    assert 'RD_ENV="$RD_ENV -e CONTEXT_ATLASSIAN_EMAIL"' in content
    assert 'RD_ENV="$RD_ENV -e CONTEXT_ATLASSIAN_TOKEN"' in content


def test_jenkinsfile_keeps_review_network_isolation_and_no_unsafe_jira_field_split():
    jenkinsfile = REPO_ROOT / "docker" / "jenkins" / "Jenkinsfile"
    content = jenkinsfile.read_text()

    assert '--network "$REVIEW_NETWORK"' in content
    assert "--network host" not in content
    assert "env.CONTEXT_JIRA_EXTRA_FIELDS.split(',')" not in content
