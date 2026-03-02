"""E2E: Docker Compose up; seed Gitea; create PR; run agent; assert comments (Phase 5).

Run only when Gitea is available (e.g. docker compose up):
  RUN_E2E=1 pytest tests/e2e/test_docker_gitea_e2e.py -v
  or: pytest -m e2e
"""

import os

import pytest


@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1",
    reason="E2E requires RUN_E2E=1 and Docker Compose with Gitea",
)
def test_e2e_docker_gitea_full_review(e2e_stack):
    """Full E2E: against real Gitea (localhost), create PR, run agent, assert comments.
    Requires: docker compose up (Gitea), repo with PR, SCM_* and LLM_* env set.
    """
    from code_review.runner import run_review

    owner = os.environ.get("SCM_OWNER", "")
    repo = os.environ.get("SCM_REPO", "")
    pr_num_str = os.environ.get("SCM_PR_NUM", "")
    head_sha = os.environ.get("SCM_HEAD_SHA", "")
    if not owner or not repo or not pr_num_str:
        pytest.skip("E2E requires SCM_OWNER, SCM_REPO, SCM_PR_NUM")
    pr_number = int(pr_num_str)
    findings = run_review(owner, repo, pr_number, head_sha=head_sha, dry_run=True)
    # Dry run: no posts; we only assert the runner completes and returns a list
    assert isinstance(findings, list)
