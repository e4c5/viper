"""ADK FunctionTools wrapping the provider. Use create_gitea_tools(provider) to get tools."""

from typing import Callable

from code_review.providers.base import ProviderInterface


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

    def get_file_content(owner: str, repo: str, ref: str, path: str) -> str:
        """Fetch file content at a given ref (branch, tag, or SHA).

        Args:
            owner: Repository owner.
            repo: Repository name.
            ref: Git ref (branch, tag, or commit SHA).
            path: File path relative to repo root.

        Returns:
            File content as string.
        """
        return provider.get_file_content(owner, repo, ref, path)

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
        provider.post_review_comments(
            owner, repo, pr_number, [(path, line, body)], head_sha=head_sha
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
        get_file_content,
        get_pr_files,
        post_review_comment,
        get_existing_review_comments,
    ]
