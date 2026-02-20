"""Tests for runner and agent (mocked provider)."""

from code_review.agent import create_review_agent
from code_review.providers.base import FileInfo


class MockProvider:
    def get_pr_files(self, owner, repo, pr_number):
        return [FileInfo(path="foo.py", status="modified")]

    def get_pr_diff(self, owner, repo, pr_number):
        return "diff --git a/foo.py b/foo.py"

    def get_file_content(self, owner, repo, ref, path):
        return "content"

    def post_review_comments(self, *args, **kwargs):
        pass

    def get_existing_review_comments(self, owner, repo, pr_number):
        return []


def test_create_review_agent():
    """Agent creation with mocked provider and review standards."""
    provider = MockProvider()
    agent = create_review_agent(provider, "### Python")
    assert agent.name == "code_review_agent"
