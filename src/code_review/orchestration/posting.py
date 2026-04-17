"""Comment posting for a single PR — the `CommentPoster` dataclass.

All methods write to the provider; none ever fetch data from it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from code_review.diff.fingerprint import format_comment_body_with_marker, parse_marker_from_comment_body
from code_review.diff.parser import iter_new_lines
from code_review.diff.utils import normalize_path as _normalize_path_for_anchor
from code_review.formatters.comment import finding_to_comment_body
from code_review.models import PRContext
from code_review.providers.base import InlineComment
from code_review.quality.outcome import QualityGateReviewOutcome
from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers (no shared state — not methods)
# ---------------------------------------------------------------------------

AGENT_VERSION: str  # set at bottom to avoid circular import bootstrap issues


def _added_lines_in_diff(diff_text: str) -> set[tuple[str, int]]:
    """Set of (normalized_path, line) for each added line in the diff."""
    out: set[tuple[str, int]] = set()
    for path, new_ln, _ in iter_new_lines(diff_text):
        out.add((_normalize_path_for_anchor(path), new_ln))
    return out


def _omit_marker_pr_summary_visible_text(
    *,
    findings_planned: int,
    successful_inline_posts: int,
    cfg,
    provider,
    gate_outcome: QualityGateReviewOutcome,
) -> str:
    """Human-readable PR summary for providers that omit inline HTML markers (e.g. Bitbucket)."""
    lines: list[str] = [
        "**Viper** (automated code review) finished for this pull request at the current revision."
    ]
    if findings_planned == 0:
        lines.append(
            "It **did not flag new issues** that require inline comments in this run "
            "(within the reviewed diff scope and your ignore rules)."
        )
        gate_in_summary = bool(getattr(cfg, "review_decision_enabled", False)) and (
            provider.capabilities().supports_review_decisions
        )
        if not gate_in_summary or gate_outcome.decision != "REQUEST_CHANGES":
            lines.append(
                "**From this automated pass, the change appears to meet expectations** "
                "for the areas reviewed."
            )
    else:
        lines.append(f"It **identified {findings_planned} issue(s)** worth addressing on the diff.")
        if successful_inline_posts >= findings_planned:
            lines.append(f"**Posted {successful_inline_posts} inline comment(s)** on the diff.")
        elif successful_inline_posts > 0:
            lines.append(
                f"**Posted {successful_inline_posts} of {findings_planned} inline comment(s)**; "
                "some could not be anchored (see CI logs)."
            )
        else:
            lines.append(
                "**Could not post inline comments** (e.g. anchor conflicts); see CI logs. "
                "Re-run after updating the PR or fixing the reported problems."
            )

    extra = _optional_quality_gate_summary_suffix(provider, cfg, gate_outcome)
    if extra:
        lines.append(extra)
    return "\n\n".join(lines)


def _optional_quality_gate_summary_suffix(
    provider,
    cfg,
    gate_outcome: QualityGateReviewOutcome,
) -> str:
    """Append threshold / merge-gate wording when review decisions are enabled."""
    if not bool(getattr(cfg, "review_decision_enabled", False)):
        return ""
    if not provider.capabilities().supports_review_decisions:
        return ""
    high_threshold = int(getattr(cfg, "review_decision_high_threshold", 1))
    medium_threshold = int(getattr(cfg, "review_decision_medium_threshold", 3))
    if gate_outcome.decision == "REQUEST_CHANGES":
        return (
            f"Given your configured thresholds, Viper **suggests this PR needs work** before merge "
            f"(open high={gate_outcome.high_count} vs threshold {high_threshold}, "
            f"open medium={gate_outcome.medium_count} vs threshold {medium_threshold})."
        )
    return (
        f"Given your configured thresholds, this PR **passes Viper's automated merge gate** "
        f"(open high={gate_outcome.high_count}, open medium={gate_outcome.medium_count})."
    )


def _generate_auto_pr_description(title: str, paths: list[str], max_files: int = 10) -> str:
    """Build a non-empty, deterministic PR description when the user did not add one."""
    title_str = title.strip() or "Untitled change"
    unique_paths = list(dict.fromkeys(paths))
    shown_paths = unique_paths[:max_files]
    files_part = ", ".join(f"`{p}`" for p in shown_paths) if shown_paths else "no files detected"
    more_suffix = ""
    if len(unique_paths) > max_files:
        more_suffix = f", and {len(unique_paths) - max_files} more file(s)"
    out = (
        f"**Title**: {title_str}\n\n"
        f"This pull request updates {len(unique_paths)} file(s): {files_part}{more_suffix}."
    )
    return out.strip() or "Auto-generated summary."


# ---------------------------------------------------------------------------
# CommentPoster — OOP boundary: fixed (provider, pr_ctx) injected once
# ---------------------------------------------------------------------------

@dataclass
class CommentPoster:
    """Posts review artefacts (inline comments, PR summaries) to one specific PR.

    Boundary rule: this class only *writes* to the provider — it never fetches
    data from it. The caller is responsible for fetching existing comments,
    diff text, etc. and passing them in as arguments.
    """

    provider: object  # BaseProvider — avoid circular import with providers/base.py
    pr_ctx: PRContext

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def post_started_review_comment(self, pr_info, paths: list[str]) -> None:
        """When PR has no description: auto-generate one and post a started-review note."""
        if not pr_info or not paths:
            return
        description = (getattr(pr_info, "description", "") or "").strip()
        if description:
            return
        generated = _generate_auto_pr_description(getattr(pr_info, "title", "") or "", paths)
        if not generated or not generated.strip():
            return
        description_updated = False
        try:
            self.provider.update_pr_description(
                self.pr_ctx.owner, self.pr_ctx.repo, self.pr_ctx.pr_number, generated
            )
            description_updated = True
        except NotImplementedError:
            pass
        except Exception as e:  # pragma: no cover
            logger.warning(
                "update_pr_description failed owner=%s repo=%s pr_number=%s: %s",
                self.pr_ctx.owner, self.pr_ctx.repo, self.pr_ctx.pr_number, e,
            )
        if description_updated:
            notes = (
                "Viper has started a review of this pull request and updated the "
                "PR description with an auto-generated summary."
            )
        else:
            notes = (
                "Viper has started a review of this pull request.\n\n"
                "The PR had no description and this SCM does not support updating it; "
                "below is the summary we generated for context:\n\n"
                f"{generated}"
            )
        try:
            self.provider.post_pr_summary_comment(
                self.pr_ctx.owner, self.pr_ctx.repo, self.pr_ctx.pr_number, notes
            )
        except Exception as e:  # pragma: no cover
            logger.warning(
                "post_pr_summary_comment (started review) failed owner=%s repo=%s pr_number=%s: %s",
                self.pr_ctx.owner, self.pr_ctx.repo, self.pr_ctx.pr_number, e,
            )

    def post_pr_summary(self, body: str) -> None:
        """Post a high-level PR summary comment (e.g. generated by the Summary Agent)."""
        if not body or not body.strip():
            return
        try:
            self.provider.post_pr_summary_comment(
                self.pr_ctx.owner, self.pr_ctx.repo, self.pr_ctx.pr_number, body
            )
        except Exception as e:
            logger.warning(
                "post_pr_summary failed owner=%s repo=%s pr_number=%s: %s",
                self.pr_ctx.owner, self.pr_ctx.repo, self.pr_ctx.pr_number, e,
            )

    def resolve_stale(
        self,
        existing: list,
        to_post: list[tuple[FindingV1, str]],
        dry_run: bool,
    ) -> None:
        """If provider supports it, resolve comments whose fingerprint is no longer in to_post."""
        if not (
            self.provider.capabilities().resolvable_comments
            and self.pr_ctx.head_sha
            and not dry_run
        ):
            return
        new_fps = {fp for _, fp in to_post if fp}
        for c in existing:
            body = getattr(c, "body", "") or ""
            parsed = parse_marker_from_comment_body(body)
            fp_old = parsed.get("fingerprint")
            if not fp_old or fp_old in new_fps:
                continue
            try:
                self.provider.resolve_comment(
                    self.pr_ctx.owner, self.pr_ctx.repo, c.id
                )
            except Exception as e:
                logger.warning(
                    "resolve_comment failed owner=%s repo=%s pr_number=%s comment_id=%s: %s",
                    self.pr_ctx.owner, self.pr_ctx.repo, self.pr_ctx.pr_number,
                    getattr(c, "id", ""), e,
                )

    def post_inline(
        self,
        incremental_base_sha: str,
        to_post: list[tuple[FindingV1, str]],
        cfg,
        llm_cfg,
        full_diff: str = "",
    ) -> int:
        """Build inline comments and post each one individually. Returns successful post count."""
        import code_review as _pkg
        agent_version = getattr(_pkg, "__version__", "0.1.0")

        caps = self.provider.capabilities()
        run_id = self.pr_ctx.idempotency_key(cfg, llm_cfg, incremental_base_sha)
        added_set = _added_lines_in_diff(full_diff) if full_diff else set()
        comments: list[InlineComment] = []
        for f, fp in to_post:
            body = finding_to_comment_body(
                f, use_collapsible_prompt=caps.markup_supports_collapsible
            )
            if fp and not caps.omit_fingerprint_marker_in_body:
                body = format_comment_body_with_marker(
                    body,
                    fp,
                    agent_version,
                    run_id=run_id,
                    marker_at_end=not caps.markup_hides_html_comment,
                )
            line_type: str | None = None
            if added_set:
                norm_path = _normalize_path_for_anchor(f.path)
                line_type = "ADDED" if (norm_path, f.line) in added_set else "CONTEXT"
            patch = f.suggested_patch
            if (
                patch
                and not caps.supports_multiline_suggestions
                and len(patch.splitlines()) > 1
            ):
                logger.warning(
                    "Stripping multiline suggested_patch from %s:%d: "
                    "platform does not support multiline suggestions",
                    f.path,
                    f.line,
                )
                patch = None
            comments.append(
                InlineComment(
                    path=f.path,
                    line=f.line,
                    body=body,
                    end_line=f.end_line,
                    suggested_patch=patch,
                    line_type=line_type,
                )
            )
        return self._post_comments_one_by_one(comments)

    def post_omit_marker_summary(
        self,
        cfg,
        llm_cfg,
        incremental_base_sha: str = "",
        *,
        findings_planned: int,
        successful_inline_posts: int,
        gate_outcome: QualityGateReviewOutcome,
        include_run_marker: bool = True,
    ) -> None:
        """Post a PR-level summary for omit-marker providers; optionally attach the run= id marker."""
        import code_review as _pkg
        agent_version = getattr(_pkg, "__version__", "0.1.0")

        caps = self.provider.capabilities()
        visible = _omit_marker_pr_summary_visible_text(
            findings_planned=findings_planned,
            successful_inline_posts=successful_inline_posts,
            cfg=cfg,
            provider=self.provider,
            gate_outcome=gate_outcome,
        )
        if include_run_marker:
            run_id = self.pr_ctx.idempotency_key(cfg, llm_cfg, incremental_base_sha)
            use_linkref = getattr(caps, "embed_agent_marker_as_commonmark_linkref", None) is True
            body = format_comment_body_with_marker(
                visible,
                "",
                agent_version,
                run_id=run_id,
                marker_at_end=not caps.markup_hides_html_comment,
                use_commonmark_linkref=use_linkref,
            )
        else:
            body = visible
        try:
            self.provider.post_pr_summary_comment(
                self.pr_ctx.owner, self.pr_ctx.repo, self.pr_ctx.pr_number, body
            )
        except Exception as e:
            logger.warning(
                "_post_omit_marker_pr_summary_comment failed owner=%s repo=%s pr_number=%s: %s",
                self.pr_ctx.owner, self.pr_ctx.repo, self.pr_ctx.pr_number, e,
            )

    # ------------------------------------------------------------------
    # Private helper
    # ------------------------------------------------------------------

    def _post_comments_one_by_one(self, comments: list[InlineComment]) -> int:
        """Post each comment individually; skip (warn) on failure. Returns successful count."""
        count = 0
        for c in comments:
            try:
                self.provider.post_review_comments(
                    self.pr_ctx.owner,
                    self.pr_ctx.repo,
                    self.pr_ctx.pr_number,
                    [c],
                    head_sha=self.pr_ctx.head_sha,
                )
                count += 1
            except Exception as e:
                logger.warning(
                    "post_review_comment failed owner=%s repo=%s pr_number=%s path=%s line=%s: %s",
                    self.pr_ctx.owner, self.pr_ctx.repo, self.pr_ctx.pr_number,
                    c.path, c.line, e,
                )
        return count
