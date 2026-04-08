"""ADK FunctionTools wrapping the provider. Use create_gitea_tools(provider) to get tools."""

from collections.abc import Callable

from code_review.agent.tools.review_helpers import detect_language_context
from code_review.diff.parser import annotate_diff_with_line_numbers
from code_review.providers.base import ProviderInterface
from code_review.providers.safety import truncate_repo_content

# ---------------------------------------------------------------------------
# Shared tool factories
# get_file_content, get_file_lines, and get_pr_files have identical
# implementations in both create_gitea_tools and create_findings_only_tools.
# They are factored out here so changes only need to be made in one place.
# ---------------------------------------------------------------------------


def _make_get_file_content(provider: ProviderInterface) -> Callable:
    """Build the get_file_content ADK tool for the given provider."""

    def get_file_content(owner: str, repo: str, ref: str, path: str) -> str:
        """Fetch file content at a given ref (branch, tag, or SHA).

        Use this for reading project-context files (e.g. AGENTS.md, README).
        For the PR diff, call get_pr_diff_for_file instead.

        Args:
            owner: Repository owner.
            repo: Repository name.
            ref: Git ref (branch, tag, or commit SHA).
            path: File path relative to repo root.

        Returns:
            File content as string (truncated to 16 KB if oversized).
        """
        return truncate_repo_content(provider.get_file_content(owner, repo, ref, path))

    return get_file_content


def _make_get_file_lines(provider: ProviderInterface) -> Callable:
    """Build the get_file_lines ADK tool for the given provider."""

    def get_file_lines(
        owner: str, repo: str, ref: str, path: str, start_line: int, end_line: int
    ) -> str:
        """Fetch a line range from a file at ref for surrounding context.

        Use this when you need additional context around a diff line.
        Always pass head_sha as ref so you read the file at the correct revision.
        Line numbers here are 1-based new-file line numbers, matching the
        ``n:`` annotations in the diff.

        Args:
            owner: Repository owner.
            repo: Repository name.
            ref: Git ref (branch or commit SHA; use head_sha from the user message).
            path: File path.
            start_line: Start line (1-based inclusive).
            end_line: End line (1-based inclusive).

        Returns:
            Lines start_line..end_line as string.
        """
        return provider.get_file_lines(owner, repo, ref, path, start_line, end_line)

    return get_file_lines


def _make_get_pr_files(provider: ProviderInterface) -> Callable:
    """Build the get_pr_files ADK tool for the given provider."""

    def get_pr_files(owner: str, repo: str, pr_number: int) -> list[dict]:
        """List files changed in a pull request.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.

        Returns:
            List of dicts with path, status, additions, deletions.
        """
        files = provider.get_pr_files(owner, repo, pr_number)
        return [f.model_dump() for f in files]

    return get_pr_files


def create_gitea_tools(provider: ProviderInterface) -> list[Callable]:
    """
    Return a list of ADK-compatible tool functions that use the given provider.
    Tools receive owner, repo, pr_number from the LLM (from user message context).
    """

    def get_pr_diff(owner: str, repo: str, pr_number: int) -> str:
        """Fetch the unified diff for a pull request.

        Args:
            owner: Repository owner or organization.
            repo: Repository name.
            pr_number: Pull request number.

        Returns:
            The unified diff string.
        """
        return provider.get_pr_diff(owner, repo, pr_number)

    def get_pr_diff_for_file(owner: str, repo: str, pr_number: int, path: str) -> str:
        """Fetch the unified diff for a single file in the PR.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            path: File path relative to repo root.

        Returns:
            Unified diff string for that file only.
        """
        return provider.get_pr_diff_for_file(owner, repo, pr_number, path)

    def post_review_comment(
        owner: str,
        repo: str,
        pr_number: int,
        path: str,
        line: int,
        body: str,
        head_sha: str = "",
    ) -> str:
        """Post an inline review comment on a specific line.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            path: File path.
            line: Line number (1-based).
            body: Comment body. Use [Critical], [Suggestion], or [Info] prefix.
            head_sha: Optional head commit SHA for the PR.

        Returns:
            Confirmation message.
        """
        from code_review.providers.base import InlineComment

        provider.post_review_comments(
            owner,
            repo,
            pr_number,
            [InlineComment(path=path, line=line, body=body)],
            head_sha=head_sha,
        )
        return f"Posted comment on {path}:{line}"

    def get_existing_review_comments(owner: str, repo: str, pr_number: int) -> list[dict]:
        """Fetch existing review comments (including resolved) for ignore list.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.

        Returns:
            List of comment dicts with id, path, line, body, resolved.
        """
        comments = provider.get_existing_review_comments(owner, repo, pr_number)
        return [c.model_dump() for c in comments]

    return [
        get_pr_diff,
        get_pr_diff_for_file,
        _make_get_file_content(provider),
        _make_get_file_lines(provider),
        _make_get_pr_files(provider),
        post_review_comment,
        get_existing_review_comments,
        detect_language_context,
    ]


def create_findings_only_tools(provider: ProviderInterface) -> list[Callable]:
    """
    Tools for agent that only returns findings (no post, no get_existing).
    Runner will fetch comments, filter, and post.

    Note: get_pr_diff (full-PR diff) is intentionally excluded. In prepared
    batch review the relevant diff is already embedded in the user message, so
    fetching it again would double the token cost. In tool-enabled review the
    agent should use get_pr_diff_for_file instead; including get_pr_diff here
    risks fetching the entire multi-hundred-kilobyte diff unnecessarily.
    """

    def get_pr_diff_for_file(owner: str, repo: str, pr_number: int, path: str) -> str:
        """Fetch the unified diff for a single file in the PR, annotated with line numbers.

        The returned diff has ``n:`` prefixes on every visible new-file line
        (added ``+`` and context `` `` lines).  Removed lines (``-``) have no
        annotation.  Use the ``n:`` value directly as the ``line`` field in
        findings — do NOT compute line numbers from hunk headers.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            path: File path relative to repo root.

        Returns:
            Line-annotated unified diff string for that file only.
        """
        return annotate_diff_with_line_numbers(
            provider.get_pr_diff_for_file(owner, repo, pr_number, path)
        )

    return [
        get_pr_diff_for_file,
        _make_get_file_content(provider),
        _make_get_file_lines(provider),
        _make_get_pr_files(provider),
        detect_language_context,
    ]
