"""CommentManager: loads existing review comments and filters duplicate findings."""

from __future__ import annotations

import hashlib
import logging

from code_review.diff.fingerprint import parse_marker_from_comment_body
from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)


def _build_ignore_set(comments: list) -> set[tuple[str, str]]:
    """Build set of (path, key) from existing review comments.

    Key is fingerprint (from marker) or body_hash for dedup and manually-resolved ignore.
    """
    out: set[tuple[str, str]] = set()
    for c in comments:
        path = getattr(c, "path", None) or (c.get("path") if isinstance(c, dict) else "")
        body = getattr(c, "body", None) or (c.get("body") if isinstance(c, dict) else "")
        if not path or not body:
            continue
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        out.add((path, body_hash))
        parsed = parse_marker_from_comment_body(body)
        if parsed.get("fingerprint"):
            out.add((path, parsed["fingerprint"]))
    return out


def _should_skip_finding_for_dedup(
    path: str,
    body_hash: str,
    fp: str,
    ignore_set: set[tuple[str, str]],
    resolved_body_set: set[tuple[str, str]],
    resolved_fp_set: set[tuple[str, str]],
) -> bool:
    """Return True if this finding should be skipped (duplicate or resolved)."""
    if fp and (path, fp) in resolved_fp_set:
        return True
    if (path, body_hash) in resolved_body_set:
        return True
    if (path, body_hash) in ignore_set and (path, body_hash) not in resolved_body_set:
        return True
    if fp and (path, fp) in ignore_set and (path, fp) not in resolved_fp_set:
        return True
    return False


class CommentManager:
    """Manages existing comment state for duplicate detection."""

    def __init__(self) -> None:
        self.ignore_set: set[tuple[str, str]] = set()
        self.resolved_fingerprints: set[tuple[str, str]] = set()
        self._resolved_body_set: set[tuple[str, str]] = set()
        self.existing_comments: list = []

    def load_existing_comments(
        self,
        provider,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> None:
        """Fetch existing review comments and build ignore/resolved sets from markers."""
        existing = provider.get_existing_review_comments(owner, repo, pr_number)
        self.existing_comments = list(existing)
        existing_dicts = [c.model_dump() for c in existing]
        self.ignore_set = _build_ignore_set(existing_dicts)

        resolved_comments = []
        for c in existing:
            resolved_flag = getattr(c, "resolved", False)
            if isinstance(resolved_flag, bool) and resolved_flag:
                resolved_comments.append(c)

        self._resolved_body_set = set()
        self.resolved_fingerprints = set()
        for c in resolved_comments:
            path = getattr(c, "path", "") or ""
            body = getattr(c, "body", "") or ""
            if not path or not body:
                continue
            body_hash = hashlib.sha256(body.encode()).hexdigest()
            self._resolved_body_set.add((path, body_hash))
            parsed = parse_marker_from_comment_body(body)
            if parsed.get("fingerprint"):
                self.resolved_fingerprints.add((path, parsed["fingerprint"]))

    def filter_duplicates(
        self,
        findings: list[FindingV1],
        fingerprint_fn,
        *,
        use_collapsible_prompt: bool = True,
    ) -> list[tuple[FindingV1, str]]:
        """Attach fingerprints and filter duplicates/resolved findings.

        ``fingerprint_fn`` should accept a ``FindingV1`` and return a fingerprint string.
        Mutates ``ignore_set`` (adds new keys for findings that will be posted).
        Returns list of ``(finding, fingerprint)`` pairs to post.
        """
        from code_review.formatters.comment import finding_to_comment_body

        to_post: list[tuple[FindingV1, str]] = []
        for f in findings:
            body = finding_to_comment_body(
                f,
                use_collapsible_prompt=use_collapsible_prompt,
            )
            body_hash = hashlib.sha256(body.encode()).hexdigest()
            fp = fingerprint_fn(f)
            if _should_skip_finding_for_dedup(
                f.path,
                body_hash,
                fp,
                self.ignore_set,
                self._resolved_body_set,
                self.resolved_fingerprints,
            ):
                continue
            if fp:
                self.ignore_set.add((f.path, fp))
            else:
                self.ignore_set.add((f.path, body_hash))
            to_post.append((f, fp))
        return to_post
