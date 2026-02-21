"""Tests that SCM tools call provider with correct arguments."""

from unittest.mock import MagicMock

from code_review.agent.tools.gitea_tools import (
    create_findings_only_tools,
    create_gitea_tools,
)
from code_review.providers.base import FileInfo


def _mock_provider():
    p = MagicMock()
    p.get_pr_diff.return_value = "diff content"
    p.get_pr_diff_for_file.return_value = "file diff"
    p.get_file_content.return_value = "file content"
    p.get_file_lines.return_value = "line1\nline2"
    p.get_pr_files.return_value = [
        FileInfo(path="a.py", status="modified", additions=1, deletions=0),
    ]
    p.get_existing_review_comments.return_value = []
    return p


def test_get_pr_diff_calls_provider():
    provider = _mock_provider()
    tools = create_gitea_tools(provider)
    get_pr_diff = next(t for t in tools if t.__name__ == "get_pr_diff")
    result = get_pr_diff("org", "repo", 42)
    provider.get_pr_diff.assert_called_once_with("org", "repo", 42)
    assert result == "diff content"


def test_get_pr_diff_for_file_calls_provider():
    provider = _mock_provider()
    tools = create_gitea_tools(provider)
    get_pr_diff_for_file = next(
        t for t in tools if t.__name__ == "get_pr_diff_for_file"
    )
    result = get_pr_diff_for_file("org", "repo", 7, "src/foo.py")
    provider.get_pr_diff_for_file.assert_called_once_with(
        "org", "repo", 7, "src/foo.py"
    )
    assert result == "file diff"


def test_get_file_content_calls_provider():
    provider = _mock_provider()
    tools = create_gitea_tools(provider)
    get_file_content = next(t for t in tools if t.__name__ == "get_file_content")
    result = get_file_content("o", "r", "main", "README.md")
    provider.get_file_content.assert_called_once_with("o", "r", "main", "README.md")
    assert result == "file content"


def test_get_pr_files_calls_provider_returns_dicts():
    provider = _mock_provider()
    tools = create_gitea_tools(provider)
    get_pr_files = next(t for t in tools if t.__name__ == "get_pr_files")
    result = get_pr_files("o", "r", 1)
    provider.get_pr_files.assert_called_once_with("o", "r", 1)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["path"] == "a.py" and result[0]["status"] == "modified"


def test_post_review_comment_calls_provider_with_head_sha():
    provider = _mock_provider()
    tools = create_gitea_tools(provider)
    post_review_comment = next(
        t for t in tools if t.__name__ == "post_review_comment"
    )
    from code_review.providers.base import InlineComment
    post_review_comment("o", "r", 2, "x.py", 10, "body", head_sha="abc")
    provider.post_review_comments.assert_called_once_with(
        "o", "r", 2, [InlineComment(path="x.py", line=10, body="body")], head_sha="abc"
    )


def test_findings_only_tools_include_get_pr_diff_and_for_file():
    provider = _mock_provider()
    tools = create_findings_only_tools(provider)
    names = [t.__name__ for t in tools]
    assert "get_pr_diff" in names
    assert "get_pr_diff_for_file" in names
    assert "post_review_comment" not in names
    assert "get_existing_review_comments" not in names
