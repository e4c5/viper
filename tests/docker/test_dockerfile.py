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
    p = REPO_ROOT / "docker" / "jenkins" / "Jenkinsfile"
    content = p.read_text()
    # Pipeline runs agent via container (docker or podman, possibly via runtime auto-detect)
    assert "review" in content
    assert " run " in content
    assert "docker" in content or "podman" in content or "runtime" in content
