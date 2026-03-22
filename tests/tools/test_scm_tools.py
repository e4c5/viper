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
    get_pr_diff_for_file = next(t for t in tools if t.__name__ == "get_pr_diff_for_file")
    result = get_pr_diff_for_file("org", "repo", 7, "src/foo.py")
    provider.get_pr_diff_for_file.assert_called_once_with("org", "repo", 7, "src/foo.py")
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
    post_review_comment = next(t for t in tools if t.__name__ == "post_review_comment")
    from code_review.providers.base import InlineComment

    post_review_comment("o", "r", 2, "x.py", 10, "body", head_sha="abc")
    provider.post_review_comments.assert_called_once_with(
        "o", "r", 2, [InlineComment(path="x.py", line=10, body="body")], head_sha="abc"
    )


def test_findings_only_tools_include_get_pr_diff_for_file_but_not_full_diff():
    """findings-only tools must include get_pr_diff_for_file but NOT get_pr_diff.

    get_pr_diff (full-PR diff) is excluded to prevent the agent from re-fetching
    the entire diff on every invocation:
    - In single-shot mode the diff is already embedded in the user message;
      calling get_pr_diff would duplicate it and double the token cost.
    - In file-by-file mode the agent must use get_pr_diff_for_file; allowing
      get_pr_diff risks fetching the full multi-hundred-kilobyte diff on every
      per-file session, which was the root cause of multi-million-token waste.
    """
    provider = _mock_provider()
    tools = create_findings_only_tools(provider)
    names = [t.__name__ for t in tools]
    assert "get_pr_diff" not in names, (
        "get_pr_diff must NOT be in findings-only tools to avoid redundant full-diff fetches"
    )
    assert "get_pr_diff_for_file" in names
    assert "post_review_comment" not in names
    assert "get_existing_review_comments" not in names


def test_findings_only_get_pr_diff_for_file_returns_annotated_diff():
    """get_pr_diff_for_file in findings-only tools must return a line-annotated diff.

    The annotation (<L{n}> prefixes on visible new-file lines) is critical for
    correct comment placement: without it, the LLM has to compute absolute line
    numbers from hunk headers by counting +/- lines — a calculation it frequently
    gets wrong when deletions precede the target line.
    """
    diff_with_deletion = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n+++ b/foo.py\n"
        "@@ -10,3 +10,3 @@\n"
        " context_10\n"
        "-old_11\n"
        "+new_11\n"
        " context_12\n"
    )
    provider = _mock_provider()
    provider.get_pr_diff_for_file.return_value = diff_with_deletion

    tools = create_findings_only_tools(provider)
    get_file_diff = next(t for t in tools if t.__name__ == "get_pr_diff_for_file")
    result = get_file_diff("o", "r", 1, "foo.py")

    # context_10 must be annotated as new-file line 10
    assert "<L10> context_10" in result
    # old_11 (removed) must NOT have an annotation
    assert all("<L" not in ln for ln in result.splitlines() if "-old_11" in ln)
    # new_11 (added) must be annotated as new-file line 11
    assert "<L11>+new_11" in result
    # context_12 must be annotated as new-file line 12
    assert "<L12> context_12" in result


def test_findings_only_get_pr_diff_for_file_has_annotation_docstring():
    """get_pr_diff_for_file tool must have a docstring mentioning <L{n}> annotations.

    ADK uses the function docstring to build the tool description shown to the LLM.
    Without it the LLM has no in-context reminder that the returned diff is annotated
    and that it should use <L{n}> values as line numbers, not hunk-header arithmetic.
    """
    provider = _mock_provider()
    tools = create_findings_only_tools(provider)
    get_file_diff = next(t for t in tools if t.__name__ == "get_pr_diff_for_file")
    doc = get_file_diff.__doc__ or ""
    assert "<L{n}>" in doc or "<L" in doc, (
        "get_pr_diff_for_file tool docstring must mention <L{n}> line annotations "
        "so the ADK tool description reminds the LLM to use them as line numbers"
    )
    assert "line" in doc.lower(), (
        "get_pr_diff_for_file docstring must explain that <L{n}> values are line numbers"
    )


# --- Shared tool factory tests ---


def test_shared_tool_factories_produce_identical_implementations():
    """create_gitea_tools and create_findings_only_tools must share implementations
    for get_file_content, get_file_lines, and get_pr_files.

    These three tools are extracted into module-level private factory helpers
    (_make_get_file_content, _make_get_file_lines, _make_get_pr_files) so that
    changes to their logic or docstrings only need to be made in one place.
    """
    import code_review.agent.tools.gitea_tools as _gtmod  # noqa: PLC0415

    provider = _mock_provider()

    # Both tool sets must have the same-named shared tools
    full_tools = create_gitea_tools(provider)
    findings_tools = create_findings_only_tools(provider)

    for name in ("get_file_content", "get_file_lines", "get_pr_files"):
        full_fn = next((t for t in full_tools if t.__name__ == name), None)
        findings_fn = next((t for t in findings_tools if t.__name__ == name), None)
        assert full_fn is not None, f"create_gitea_tools must have {name}"
        assert findings_fn is not None, f"create_findings_only_tools must have {name}"
        # Both must come from the same factory (same __qualname__ pattern)

        factory_fn = getattr(_gtmod, f"_make_{name}")
        reference = factory_fn(provider)
        assert full_fn.__qualname__ == reference.__qualname__, (
            f"{name} in create_gitea_tools must come from the shared factory"
        )
        assert findings_fn.__qualname__ == reference.__qualname__, (
            f"{name} in create_findings_only_tools must come from the shared factory"
        )


def test_shared_get_file_content_calls_provider_in_gitea_tools():
    """get_file_content from create_gitea_tools must delegate to provider."""
    provider = _mock_provider()
    tools = create_gitea_tools(provider)
    fn = next(t for t in tools if t.__name__ == "get_file_content")
    result = fn("o", "r", "main", "README.md")
    provider.get_file_content.assert_called_once_with("o", "r", "main", "README.md")
    assert result == "file content"


def test_shared_get_file_lines_calls_provider_in_both_tool_sets():
    """get_file_lines must delegate to provider in both tool sets."""
    for factory in (create_gitea_tools, create_findings_only_tools):
        provider = _mock_provider()
        tools = factory(provider)
        fn = next(t for t in tools if t.__name__ == "get_file_lines")
        result = fn("o", "r", "abc", "foo.py", 5, 10)
        provider.get_file_lines.assert_called_once_with("o", "r", "abc", "foo.py", 5, 10)
        assert result == "line1\nline2"


def test_shared_get_pr_files_calls_provider_in_both_tool_sets():
    """get_pr_files must return model_dump() dicts in both tool sets."""
    for factory in (create_gitea_tools, create_findings_only_tools):
        provider = _mock_provider()
        tools = factory(provider)
        fn = next(t for t in tools if t.__name__ == "get_pr_files")
        result = fn("o", "r", 99)
        provider.get_pr_files.assert_called_once_with("o", "r", 99)
        assert result == [{"path": "a.py", "status": "modified", "additions": 1, "deletions": 0}]
