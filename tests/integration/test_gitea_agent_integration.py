"""
Integration test: full runner + real GiteaProvider against mocked Gitea API.

Uses respx to mock Gitea HTTP so no Docker is required. Asserts that when the
agent (mocked) returns findings, the runner posts them via the provider to the
Gitea API (POST /repos/.../pulls/.../reviews).
"""

import base64
import json
import re
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests.conftest import runner_run_async_returning

try:
    import respx
except ImportError:
    respx = None

# Use a hostname without dots so respx exact URL matching works (dots can be regex)
BASE = "http://gitea-test"
API = f"{BASE}/api/v1"

# Minimal unified diff for one file
SAMPLE_DIFF = """diff --git a/foo.py b/foo.py
index 123..456 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
+import os
 def main():
     pass
"""


@pytest.mark.skipif(respx is None, reason="respx required for integration test")
@pytest.mark.respx(assert_all_mocked=True, assert_all_called=False)
@patch("code_review.runner.get_scm_config")
@patch("code_review.runner.get_provider")
@patch("code_review.runner.get_context_window", return_value=1_000_000)
@patch("code_review.runner.get_llm_config")
@patch("google.adk.runners.Runner")
def test_agent_vs_gitea_posts_findings_to_mocked_api(
    mock_runner_class, mock_llm, mock_context_window, mock_get_provider, mock_cfg, respx_mock
):
    """Run full run_review against mocked Gitea; assert POST review is called with findings."""
    from code_review.providers.gitea import GiteaProvider
    from code_review.runner import run_review

    owner, repo, pr_number, head_sha = "o", "r", 1, "abc123"

    # Mock Gitea API. Match by path (path__regex) so we don't depend on full URL string form.
    comments_path = re.compile(r"^/api/v1/repos/o/r/pulls/1/comments$")
    files_path = re.compile(r"^/api/v1/repos/o/r/pulls/1/files$")
    diff_path = re.compile(r"^/api/v1/repos/o/r/pulls/1\.diff$")
    pr_path = re.compile(r"^/api/v1/repos/o/r/pulls/1$")
    contents_path = re.compile(r"^/api/v1/repos/o/r/contents/foo\.py")
    reviews_path = re.compile(r"^/api/v1/repos/o/r/pulls/1/reviews$")
    # PR summary comment (Phase 4.2): Gitea uses issues/{pr}/comments
    issues_comments_path = re.compile(r"^/api/v1/repos/o/r/issues/1/comments$")

    respx_mock.get(path__regex=comments_path).mock(return_value=httpx.Response(200, json=[]))
    respx_mock.get(path__regex=files_path).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "filename": "foo.py",
                    "path": "foo.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                }
            ],
        )
    )
    respx_mock.get(path__regex=diff_path).mock(return_value=httpx.Response(200, text=SAMPLE_DIFF))
    respx_mock.get(path__regex=pr_path).mock(
        return_value=httpx.Response(200, json={"title": "PR", "labels": []})
    )
    respx_mock.get(path__regex=contents_path).mock(
        return_value=httpx.Response(
            200,
            json={"content": base64.b64encode(b"import os\ndef main():\n    pass\n").decode()},
        )
    )
    post_review = respx_mock.post(path__regex=reviews_path).mock(
        return_value=httpx.Response(200, json={})
    )
    post_summary = respx_mock.post(path__regex=issues_comments_path).mock(
        return_value=httpx.Response(200, json={})
    )

    mock_cfg.return_value = MagicMock(
        provider="gitea",
        url=BASE,
        token="test-token",
        skip_label="",
        skip_title_pattern="",
    )
    mock_llm.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")
    mock_get_provider.return_value = GiteaProvider(base_url=BASE, token="test-token")

    findings_json = """[
        {
            "path": "foo.py",
            "line": 2,
            "severity": "suggestion",
            "code": "unused-import",
            "message": "Remove unused import os."
        }
    ]"""
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=findings_json)]
    mock_runner_instance = MagicMock()
    mock_runner_instance.run_async = runner_run_async_returning([mock_event])
    mock_runner_class.return_value = mock_runner_instance

    to_post = run_review(owner, repo, pr_number, head_sha=head_sha, dry_run=False)

    assert len(to_post) == 1
    assert to_post[0].path == "foo.py"
    assert "unused" in to_post[0].message.lower() or "os" in to_post[0].message

    assert post_review.called
    call = post_review.calls[0]
    assert call.request.url.path == f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    payload = json.loads(call.request.content.decode())
    assert "comments" in payload
    assert len(payload["comments"]) == 1
    comment = payload["comments"][0]
    assert comment["path"] == "foo.py"
    # Gitea CreatePullReviewComment uses old_position/new_position for diff coordinates.
    assert comment["old_position"] == 0
    assert comment["new_position"] == 2
    assert "[Suggestion]" in comment["body"]
    assert payload.get("commit_id") == head_sha

    # Phase 4.2: PR summary comment posted after successful inline post.
    # The runner may also post an initial "Viper has started a review" comment when
    # the PR description is missing; assert that at least one summary comment was made
    # and that the final one contains the aggregated summary text.
    assert post_summary.called
    summary_payload = json.loads(post_summary.calls[-1].request.content.decode())
    assert "body" in summary_payload
    assert "1 Suggestion" in summary_payload["body"]
    assert "See inline comments above" in summary_payload["body"]
