"""Tests for the diff-line guardrail: findings outside diff hunks are filtered out.

Root cause of the inline comment bug:
  In single-shot mode the LLM receives the full multi-file diff and can report
  findings for lines that appear in the FILE but not in any diff hunk.  When such
  a line is sent to Bitbucket Cloud via the inline-comment API, the API either
  rejects it (raising an exception that causes a fallback to post_pr_summary_comment
  with the **path:line** format visible only in the Activity feed) or creates a
  non-inline comment.  File-by-file mode avoids this because the LLM only sees
  each individual file's diff and naturally reports lines within the hunks.

Fix: _diff_visible_new_lines() builds the set of visible lines; the runner uses it
to drop any finding whose line is not in that set before posting.
"""

from unittest.mock import MagicMock, patch

from code_review.providers.base import FileInfo
from code_review.runner import _diff_visible_new_lines, _normalize_path_for_anchor
from tests.conftest import runner_run_async_returning

# A minimal unified diff that changes only lines 10-11 of foo.py.
# Context lines 8, 9, 12, 13 are also visible; line 1 and line 99 are NOT.
SAMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -8,5 +8,6 @@
 context_line_8
 context_line_9
-old_line_10
+new_line_10
+new_line_11
 context_line_12
 context_line_13
"""


def test_diff_visible_new_lines_includes_added_and_context():
    """Both ADDED (+) and CONTEXT ( ) lines from the new file should be in the set."""
    visible = _diff_visible_new_lines(SAMPLE_DIFF)
    norm = _normalize_path_for_anchor("foo.py")
    # Context lines visible in the hunk
    assert (norm, 8) in visible, "context line 8 should be visible"
    assert (norm, 9) in visible, "context line 9 should be visible"
    # Added lines
    assert (norm, 10) in visible, "added line 10 should be visible"
    assert (norm, 11) in visible, "added line 11 should be visible"
    assert (norm, 12) in visible, "context line 12 should be visible"
    assert (norm, 13) in visible, "context line 13 should be visible"


def test_diff_visible_new_lines_excludes_removed_lines():
    """Removed (-) lines have no new-file line number; must NOT be in the set."""
    visible = _diff_visible_new_lines(SAMPLE_DIFF)
    norm = _normalize_path_for_anchor("foo.py")
    # old_line_10 is a removed line; it doesn't appear in the new file at any position
    # in the visible set (it maps to old_ln, not new_ln).
    # Lines 1 and 99 are not in any hunk at all.
    assert (norm, 1) not in visible, "line 1 is not in any diff hunk"
    assert (norm, 99) not in visible, "line 99 is not in any diff hunk"


def test_diff_visible_new_lines_empty_diff():
    """Empty diff → empty set (no crash)."""
    visible = _diff_visible_new_lines("")
    assert visible == set()


def test_diff_visible_new_lines_normalizes_paths():
    """Paths with a/ or b/ prefixes are normalized the same as finding paths."""
    diff = """\
diff --git a/src/foo.py b/src/foo.py
--- a/src/foo.py
+++ b/src/foo.py
@@ -5,3 +5,4 @@
 ctx
+added_line
 ctx2
"""
    visible = _diff_visible_new_lines(diff)
    norm = _normalize_path_for_anchor("src/foo.py")
    assert (norm, 5) in visible
    assert (norm, 6) in visible
    assert (norm, 7) in visible


# ---------------------------------------------------------------------------
# Integration: runner drops findings for lines outside diff hunks
# ---------------------------------------------------------------------------

_FINDINGS_WITH_OUT_OF_DIFF_LINE = (
    '{"findings":['
    '{"path":"foo.py","line":1,"severity":"low","code":"c","message":"line 1 not in diff"},'
    '{"path":"foo.py","line":10,"severity":"low","code":"d","message":"line 10 is in diff"}'
    ']}'
)


def _run_review_with_mocked_bitbucket_runner(
    mock_scm,
    mock_get_provider,
    mock_llm,
    mock_context_window,
    findings_json: str,
    head_sha: str,
):
    """Shared setup for Bitbucket runner guardrail tests."""
    from code_review.runner import run_review

    mock_scm.return_value = MagicMock(
        provider="bitbucket",
        url="https://api.bitbucket.org/2.0",
        token="x",
        skip_label="",
        skip_title_pattern="",
    )
    mock_llm.return_value = MagicMock(provider="gemini", model="gemini-2.5-flash")

    provider = MagicMock()
    provider.get_pr_files.return_value = [FileInfo(path="foo.py", status="modified")]
    provider.get_pr_diff.return_value = SAMPLE_DIFF
    provider.get_file_content.return_value = "\n" * 20
    provider.get_existing_review_comments.return_value = []
    provider.capabilities.return_value = MagicMock(
        resolvable_comments=False,
        supports_suggestions=False,
        markup_hides_html_comment=False,
        markup_supports_collapsible=False,
        omit_fingerprint_marker_in_body=True,
    )
    mock_get_provider.return_value = provider
    mock_context_window.return_value = 1_000_000  # large → single-shot mode

    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = MagicMock()
    mock_event.content.parts = [MagicMock(text=findings_json)]
    mock_runner = MagicMock()
    mock_runner.run_async = runner_run_async_returning([mock_event])

    with patch("google.adk.runners.Runner", return_value=mock_runner):
        return run_review("owner", "repo", 1, head_sha=head_sha, dry_run=True)


@patch("code_review.orchestration_deps.get_context_window")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_scm_config")
def test_runner_drops_findings_for_lines_outside_diff(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
) -> None:
    """Runner must filter out findings for lines not visible in the diff.

    Without this guardrail, Bitbucket Cloud receives an inline comment request for
    a line that's not in any diff hunk.  The API rejects it (4xx), the runner falls
    back to post_pr_summary_comment, and the comment appears in the Activity feed
    with a **path:line** heading instead of inline in the diff view.
    """

    # Diff only covers lines 8-13 in the new file; line 1 is outside all hunks.
    findings = _run_review_with_mocked_bitbucket_runner(
        mock_scm,
        mock_get_provider,
        mock_llm,
        mock_context_window,
        _FINDINGS_WITH_OUT_OF_DIFF_LINE,
        head_sha="sha1",
    )

    # Only the finding on line 10 (visible in the diff) should survive.
    assert len(findings) == 1, (
        "Finding on line 1 (outside diff hunks) must be dropped by the diff-line guardrail; "
        "finding on line 10 (inside diff hunk) must be kept"
    )
    assert findings[0].line == 10


@patch("code_review.orchestration_deps.get_context_window")
@patch("code_review.orchestration_deps.get_llm_config")
@patch("code_review.orchestration_deps.get_provider")
@patch("code_review.orchestration_deps.get_scm_config")
def test_runner_keeps_context_line_findings(
    mock_scm, mock_get_provider, mock_llm, mock_context_window
) -> None:
    """Context (unchanged) lines shown in the diff should NOT be filtered out."""

    # Line 8 is a context line visible in the diff — it should NOT be filtered.
    context_line_finding = (
        '{"findings":[{"path":"foo.py","line":8,"severity":"medium","code":"c","message":"context line issue"}]}'
    )

    findings = _run_review_with_mocked_bitbucket_runner(
        mock_scm,
        mock_get_provider,
        mock_llm,
        mock_context_window,
        context_line_finding,
        head_sha="sha2",
    )

    assert len(findings) == 1, "Context-line finding must be kept (context lines are diff-visible)"
    assert findings[0].line == 8
