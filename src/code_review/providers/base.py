"""Abstract provider interface for SCM backends (Gitea, GitLab, Bitbucket)."""

import logging
from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from code_review.diff.parser import parse_unified_diff


def _log_pr_info_warning(
    logger: logging.Logger,
    owner: str,
    repo: str,
    pr_number: int,
    exc: Exception,
) -> None:
    """Emit a standardised warning when get_pr_info fails and return None.

    Centralised so the identical warning block is not repeated in every provider.
    """
    logger.warning(
        "get_pr_info failed owner=%s repo=%s pr_number=%s: %s",
        owner,
        repo,
        pr_number,
        exc,
    )


def _log_pr_commit_messages_warning(
    logger: logging.Logger,
    owner: str,
    repo: str,
    pr_number: int,
    exc: Exception,
) -> None:
    """Emit a standardised warning when get_pr_commit_messages fails."""
    logger.warning(
        "get_pr_commit_messages failed owner=%s repo=%s pr_number=%s: %s",
        owner,
        repo,
        pr_number,
        exc,
    )


def commit_messages_from_commit_list(data: object) -> list[str]:
    """
    Extract commit messages from provider commit-list payloads.

    Supports entries shaped like:
    - {"commit": {"message": "..."}}
    - {"message": "..."}
    """
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        commit_obj = item.get("commit")
        raw_msg = (commit_obj.get("message") if isinstance(commit_obj, dict) else None) or item.get(
            "message"
        )
        msg = str(raw_msg or "").strip()
        if msg:
            out.append(msg)
    return out


class RateLimitError(Exception):
    """Raised when the SCM API returns a 429 Too Many Requests response.

    This is a known error; callers should skip to the next task rather than
    retrying, because retrying a rate-limited request will only worsen the
    situation.
    """


class ProviderCapabilities(BaseModel):
    """
    Provider capability flags for branching behavior.
    - resolvable_comments: provider supports marking comments as resolved.
      (Currently false for all built-in providers.)
    - supports_suggestions: provider supports suggested-change / code suggestion blocks
     (e.g. GitHub, GitLab).
    - markup_hides_html_comment: when True, HTML comments (e.g. <!-- ... -->) are hidden
      in comment bodies, so the fingerprint marker can be prepended. When False (e.g. Bitbucket),
      the marker is appended so the visible part of the comment is not prefixed by raw markup.
    - markup_supports_collapsible: when True, <details>/<summary> render as collapsible
      sections. When False, the agent prompt is formatted as plain text to avoid raw tags.
    - omit_fingerprint_marker_in_body: when True, do not add the HTML comment marker to
      the comment body at all (avoids stray XML in UIs that display it). Dedup still uses
      body_hash; fingerprint cannot be read back from existing comments.
    - supports_review_decisions: provider supports PR-level review decisions
      (APPROVE / REQUEST_CHANGES).
    """

    resolvable_comments: bool = False
    supports_suggestions: bool = False
    supports_multiline_suggestions: bool = False
    markup_hides_html_comment: bool = True
    markup_supports_collapsible: bool = True
    omit_fingerprint_marker_in_body: bool = False
    supports_review_decisions: bool = False


ReviewDecision = Literal["APPROVE", "REQUEST_CHANGES"]


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
    description: str = ""


def pr_info_from_api_dict(data: dict, description_key: str = "body") -> PRInfo:
    """Build PRInfo from a provider API dict. Use description_key='description' for GitLab/Bitbucket."""
    title = data.get("title", "") or ""
    labels_raw = data.get("labels") or []
    labels = [lb.get("name", lb) if isinstance(lb, dict) else str(lb) for lb in labels_raw]
    description = data.get(description_key, "") or ""
    return PRInfo(title=title, labels=labels, description=description)


def normalize_diff_anchor_path(file_path: str) -> str:
    """Normalize a file path so it matches PR diffs across providers.

    Strips dst:// and src:// prefixes so e.g. dst://src/main/java/foo.java ->
    src/main/java/foo.java, and removes leading slashes. Falls back to the
    original value if normalization results in an empty string.
    """
    p = (file_path or "").strip()
    for prefix in ("dst://", "src://"):
        if p.lower().startswith(prefix):
            p = p[len(prefix) :].lstrip("/")
            break
    return p.lstrip("/") or file_path or ""


def file_infos_from_pull_file_list(files: list) -> list[FileInfo]:
    """Build list of FileInfo from a provider API list of file dicts (filename/path, status, additions, deletions)."""
    if not isinstance(files, list):
        return []
    result: list[FileInfo] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        result.append(
            FileInfo(
                path=f.get("filename", f.get("path", "")),
                status=f.get("status", "modified"),
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
            )
        )
    return result


class ReviewComment(BaseModel):
    """A review comment with resolved status for fingerprinting."""

    id: str
    path: str
    line: int
    body: str
    resolved: bool = False


ReviewItemKind = Literal["inline_comment", "discussion_thread", "task"]


class UnresolvedReviewItem(BaseModel):
    """Normalized unresolved review signal for PR quality-gate decisioning.

    Providers map SCM-specific threads, tasks, or inline comments into this shape.
    ``inferred_severity`` is parsed from comment text when possible (e.g. ``[High]``).
    """

    stable_id: str = Field(..., description="Unique id for this item within one provider response")
    thread_id: str | None = None
    kind: ReviewItemKind = "inline_comment"
    path: str = ""
    line: int = 0
    body: str = ""
    inferred_severity: Literal["high", "medium", "low", "nit", "unknown"] = "unknown"


def default_unresolved_review_items_from_comments(
    comments: list[ReviewComment],
) -> list[UnresolvedReviewItem]:
    """Build unresolved items from inline comments with resolved=False (shared default)."""
    from code_review.formatters.comment import infer_severity_from_comment_body

    out: list[UnresolvedReviewItem] = []
    for c in comments:
        if c.resolved:
            continue
        body = (c.body or "").strip()
        if not body:
            continue
        cid = (c.id or "").strip()
        stable = f"comment:{cid}" if cid else f"path:{c.path}:{c.line}"
        out.append(
            UnresolvedReviewItem(
                stable_id=stable,
                thread_id=None,
                kind="inline_comment",
                path=c.path or "",
                line=int(c.line or 0),
                body=body,
                inferred_severity=infer_severity_from_comment_body(body),
            )
        )
    return out


class InlineComment(BaseModel):
    """
    Provider-neutral inline review comment. Runner builds these from findings;
    each provider converts to its SCM API shape (inline, file-level, or PR-level fallback).
    When capabilities().supports_suggestions is True, providers may render suggested_patch
    as a suggestion block (e.g. GitHub/GitLab).
    line_type: when set to "ADDED" or "CONTEXT", Bitbucket Server uses it in the anchor
    so the comment attaches to the correct line in the diff (otherwise it may show as file-level).
    """

    path: str
    line: int = Field(..., ge=1, description="Line in new file (1-based)")
    body: str
    end_line: int | None = Field(
        default=None, ge=1, description="Optional end line for multi-line comments"
    )
    suggested_patch: str | None = Field(
        default=None,
        description="Optional suggested code change; used when provider supports_suggestions",
    )
    line_type: str | None = Field(
        default=None,
        description="For Bitbucket Server: 'ADDED' or 'CONTEXT' so the comment anchors to the diff line",
    )

    @model_validator(mode="after")
    def end_line_not_less_than_line(self) -> "InlineComment":
        if self.end_line is not None and self.end_line < self.line:
            raise ValueError(f"end_line ({self.end_line}) must be >= line ({self.line})")
        return self


class ProviderInterface(ABC):
    """Abstract interface for SCM providers."""

    @abstractmethod
    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return unified diff string for the PR."""
        ...

    def get_pr_diff_for_file(self, owner: str, repo: str, pr_number: int, path: str) -> str:
        """
        Return diff for a single file.

        Default implementation parses the full PR diff and slices by file path.
        Providers with native per-file diff endpoints may override for efficiency.
        """
        full_diff = self.get_pr_diff(owner, repo, pr_number)
        hunks = parse_unified_diff(full_diff)
        lines: list[str] = []
        headers_emitted = False
        for hunk in hunks:
            if hunk.path != path:
                continue
            if not headers_emitted:
                lines.append(f"--- a/{hunk.path}")
                lines.append(f"+++ b/{hunk.path}")
                headers_emitted = True
            lines.append(
                f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@"
            )
            for content, old_ln, new_ln in hunk.lines:
                if old_ln is not None and new_ln is not None:
                    lines.append(" " + content)
                elif new_ln is not None:
                    lines.append("+" + content)
                elif old_ln is not None:
                    lines.append("-" + content)
                else:
                    lines.append("\\" + content)
        return "\n".join(lines) if lines else ""

    @abstractmethod
    def get_file_content(self, owner: str, repo: str, ref: str, path: str) -> str:
        """Return file content at ref (branch/tag/SHA)."""
        ...

    def get_file_lines(
        self,
        owner: str,
        repo: str,
        ref: str,
        path: str,
        start_line: int,
        end_line: int,
    ) -> str:
        """
        Return lines start_line..end_line (1-based inclusive) from file at ref.

        Default implementation calls get_file_content and slices the result.
        Providers may override if they have a more efficient line-range API.
        """
        content = self.get_file_content(owner, repo, ref, path)
        lines = content.splitlines()
        if start_line < 1 or end_line < start_line:
            return ""
        start_idx = start_line - 1
        end_idx = min(end_line, len(lines))
        return "\n".join(lines[start_idx:end_idx])

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
        """
        Post inline comments.

        Accepts internal InlineComment; provider converts to SCM API payload.
        """
        ...

    def submit_review_decision(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        decision: ReviewDecision,
        *,
        body: str = "",
        head_sha: str = "",
    ) -> None:
        """Submit a PR-level review decision (e.g. APPROVE or REQUEST_CHANGES)."""
        raise NotImplementedError("submit_review_decision not implemented for this provider")

    def post_review_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        path: str,
        line: int,
        body: str,
        end_line: int | None = None,
        suggested_patch: str | None = None,
        head_sha: str = "",
    ) -> None:
        """Post a single inline comment. Default: call post_review_comments with one item."""
        self.post_review_comments(
            owner,
            repo,
            pr_number,
            [
                InlineComment(
                    path=path,
                    line=line,
                    body=body,
                    end_line=end_line,
                    suggested_patch=suggested_patch,
                )
            ],
            head_sha=head_sha,
        )

    @abstractmethod
    def get_existing_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[ReviewComment]:
        """Return existing review comments (include resolved status for ignore list)."""
        ...

    def get_unresolved_review_items_for_quality_gate(
        self, owner: str, repo: str, pr_number: int
    ) -> list[UnresolvedReviewItem]:
        """Return unresolved review threads/tasks/comments for approve/request-changes gating.

        Default uses ``get_existing_review_comments`` and treats ``resolved=False`` as open.
        Providers with thread- or task-level resolution should override.
        """
        return default_unresolved_review_items_from_comments(
            self.get_existing_review_comments(owner, repo, pr_number)
        )

    def resolve_comment(self, owner: str, repo: str, comment_id: str) -> None:  # noqa: B027
        """Mark a comment as resolved. Default no-op if provider lacks support."""
        pass

    def unresolve_comment(self, owner: str, repo: str, comment_id: str) -> None:  # noqa: B027
        """Mark a comment as unresolved. Optional; default no-op."""
        pass

    def post_pr_summary_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post a PR-level comment (e.g. when inline positioning fails or finding is file-level)."""
        raise NotImplementedError("post_pr_summary_comment not implemented for this provider")

    def update_pr_description(
        self, owner: str, repo: str, pr_number: int, description: str, title: str | None = None
    ) -> None:
        """
        Update the pull request description (and optionally title) in the SCM.

        Used when the PR has no meaningful description so Viper can set an
        auto-generated summary as the actual PR body instead of only posting a comment.
        Default: NotImplementedError (provider does not support updating PR description).
        """
        raise NotImplementedError("update_pr_description not implemented for this provider")

    def capabilities(self) -> ProviderCapabilities:
        """Return provider capability flags."""
        return ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=False,
            supports_multiline_suggestions=False,
        )

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> PRInfo | None:
        """
        Return PR title and labels for skip-review check.

        Default: None (skip check not supported).
        """
        return None

    def get_pr_commit_messages(self, owner: str, repo: str, pr_number: int) -> list[str]:
        """
        Return commit messages for commits in this PR/MR (oldest-first), one string per commit.

        Used for reference extraction (context-aware review) and optional prompt enrichment.
        Default: empty list (provider does not implement commit listing).
        """
        return []
