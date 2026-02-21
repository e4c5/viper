"""Abstract provider interface for SCM backends (Gitea, GitLab, Bitbucket)."""

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class ProviderCapabilities(BaseModel):
    """
    Provider capability flags for branching behavior.
    - resolvable_comments: provider supports marking comments as resolved (e.g. GitLab).
    - supports_suggestions: provider supports suggested-change / code suggestion blocks (e.g. GitHub, GitLab).
    """

    resolvable_comments: bool = False
    supports_suggestions: bool = False


class FileInfo(BaseModel):
    """Metadata for a file in a PR."""

    path: str
    status: str = Field(default="modified", description="added, removed, modified")
    additions: int = 0
    deletions: int = 0


class PRInfo(BaseModel):
    """PR metadata for skip-review and similar checks."""

    title: str = ""
    labels: list[str] = Field(default_factory=list, description="Label names")


class ReviewComment(BaseModel):
    """A review comment with resolved status for fingerprinting."""

    id: str
    path: str
    line: int
    body: str
    resolved: bool = False


class InlineComment(BaseModel):
    """
    Provider-neutral inline review comment. Runner builds these from findings;
    each provider converts to its SCM API shape (inline, file-level, or PR-level fallback).
    When capabilities().supports_suggestions is True, providers may render suggested_patch
    as a suggestion block (e.g. GitHub/GitLab).
    """

    path: str
    line: int = Field(..., ge=1, description="Line in new file (1-based)")
    body: str
    end_line: int | None = Field(default=None, ge=1, description="Optional end line for multi-line comments")
    suggested_patch: str | None = Field(default=None, description="Optional suggested code change; used when provider supports_suggestions")


class ProviderInterface(ABC):
    """Abstract interface for SCM providers."""

    @abstractmethod
    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff string for the PR."""
        ...

    @abstractmethod
    def get_pr_diff_for_file(
        self, owner: str, repo: str, pr_number: int, path: str
    ) -> str:
        """Return diff for a single file. Parse full diff and slice by file if SCM lacks per-file endpoint."""
        ...

    @abstractmethod
    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Return file content at ref (branch/tag/SHA)."""
        ...

    @abstractmethod
    def get_file_lines(
        self,
        owner: str,
        repo: str,
        ref: str,
        path: str,
        start_line: int,
        end_line: int,
    ) -> str:
        """Return lines start_line..end_line (1-based inclusive) from file at ref."""
        ...

    @abstractmethod
    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[FileInfo]:
        """Return list of changed files in the PR."""
        ...

    @abstractmethod
    def post_review_comments(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        comments: list[InlineComment],
        head_sha: str = "",
    ) -> None:
        """Post inline comments. Accepts internal InlineComment; provider converts to SCM API payload."""
        ...

    def post_review_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        path: str,
        line: int,
        body: str,
        head_sha: str = "",
    ) -> None:
        """Post a single inline comment. Default: call post_review_comments with one item."""
        self.post_review_comments(
            owner, repo, pr_number, [InlineComment(path=path, line=line, body=body)], head_sha=head_sha
        )

    @abstractmethod
    def get_existing_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewComment]:
        """Return existing review comments (include resolved status for ignore list)."""
        ...

    def resolve_comment(self, owner: str, repo: str, comment_id: str) -> None:
        """Mark a comment as resolved. Default no-op if provider lacks support."""
        pass

    def unresolve_comment(self, owner: str, repo: str, comment_id: str) -> None:
        """Mark a comment as unresolved. Optional; default no-op."""
        pass

    def post_pr_summary_comment(
        self, owner: str, repo: str, pr_number: int, body: str
    ) -> None:
        """Post a PR-level comment (e.g. when inline positioning fails or finding is file-level)."""
        raise NotImplementedError(
            "post_pr_summary_comment not implemented for this provider"
        )

    def capabilities(self) -> ProviderCapabilities:
        """Return provider capability flags."""
        return ProviderCapabilities(resolvable_comments=False, supports_suggestions=False)

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """Return PR title and labels for skip-review check. Default: None (skip check not supported)."""
        return None
