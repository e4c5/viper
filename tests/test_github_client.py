from code_review.github_client import GitHubApiClient


def test_request_text_extracts_wrapped_blob_payload():
    client = GitHubApiClient("https://api.github.com", "tok")
    requester = client._github.requester

    original = requester.requestBlobAndCheck
    try:
        requester.requestBlobAndCheck = lambda *args, **kwargs: (
            {},
            {"data": "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py"},
        )

        diff = client.request_text("GET", "/repos/owner/repo/pulls/1")
    finally:
        requester.requestBlobAndCheck = original

    assert diff.startswith("diff --git a/foo.py b/foo.py")