from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from code_review import orchestration_deps as runner_mod
from code_review.batching import build_review_batch_budget
from code_review.comments.manager import CommentManager
from code_review.models import PRContext
from code_review.orchestration import execution as execution_mod
from code_review.orchestration.context_enricher import ContextEnricher
from code_review.orchestration.posting import CommentPoster
from code_review.orchestration.prompts import _LINKED_CONTEXT_HEADER
from code_review.orchestration.review_decision import ReviewDecisionHandler
from code_review.orchestration.runner_utils import ReviewRunObservability
from code_review.providers.base import FileInfo, PRInfo, ProviderInterface
from code_review.quality.gate import QualityGate
from code_review.refinement.pipeline import FindingRefinementPipeline
from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)

_CONTEXT_TAG = _LINKED_CONTEXT_HEADER


class StandardReviewHandler:
    """Execute the full findings review workflow for a single PR."""

    def __init__(
        self,
        pr_ctx: PRContext,
        *,
        dry_run: bool,
        print_findings: bool,
        context_enricher: ContextEnricher,
        review_decision_handler: ReviewDecisionHandler,
        result_builder: Callable[..., list[FindingV1]],
    ) -> None:
        self.pr_ctx = pr_ctx
        self.dry_run = dry_run
        self.print_findings = print_findings
        self.context_enricher = context_enricher
        self.review_decision_handler = review_decision_handler
        self._result_builder = result_builder

    @property
    def owner(self) -> str:
        return self.pr_ctx.owner

    @property
    def repo(self) -> str:
        return self.pr_ctx.repo

    @property
    def pr_number(self) -> int:
        return self.pr_ctx.pr_number

    @property
    def head_sha(self) -> str:
        return self.pr_ctx.head_sha

    def fetch_review_files_and_diffs(
        self,
        provider,
        cfg,
        *,
        incremental_base_sha_fn: Callable[[Any, str], str],
    ) -> tuple[list[object], list[str], str, str]:
        """Fetch the file list and diff for the active review scope."""
        base_sha = incremental_base_sha_fn(cfg, self.head_sha)
        if base_sha:
            files = provider.get_incremental_pr_files(
                self.owner, self.repo, self.pr_number, base_sha, self.head_sha
            )
            paths = [f.path for f in files]
            full_diff = provider.get_incremental_pr_diff(
                self.owner, self.repo, self.pr_number, base_sha, self.head_sha
            )
            return (files, paths, full_diff, base_sha)
        files = provider.get_pr_files(self.owner, self.repo, self.pr_number)
        paths = [f.path for f in files]
        full_diff = provider.get_pr_diff(self.owner, self.repo, self.pr_number)
        return (files, paths, full_diff, "")

    @staticmethod
    def detect_languages_for_files(paths: list[str]):
        """Run language detection on paths and return (detected, review_standards)."""
        detected = runner_mod.detect_from_paths(paths)
        review_standards = runner_mod.get_review_standards(detected.language, detected.framework)
        return (detected, review_standards)

    def make_fingerprint_fn(self, provider):
        """Return a fingerprint function (FindingV1 -> str) backed by live file content."""
        cache: dict[str, list[str]] = {}

        def _fingerprint_fn(finding: FindingV1) -> str:
            if not self.head_sha:
                return ""
            if finding.path not in cache:
                file_lines_by_path = runner_mod._get_file_lines_by_path(
                    provider,
                    self.owner,
                    self.repo,
                    self.head_sha,
                    [finding.path],
                )
                cache[finding.path] = file_lines_by_path.get(finding.path, [])
            file_lines_by_path = {finding.path: cache[finding.path]}
            return (
                runner_mod._fingerprint_for_finding(finding, file_lines_by_path)
                if file_lines_by_path
                else ""
            )

        return _fingerprint_fn

    def post_findings_and_summary(
        self,
        provider,
        incremental_base_sha: str,
        to_post: list[tuple[FindingV1, str]],
        cfg,
        llm_cfg,
        existing: list,
        full_diff: str = "",
    ) -> int:
        """
        Auto-resolve stale comments (if supported), then post inline comments.
        Returns successful_post_count.
        """
        poster = CommentPoster(provider, self.pr_ctx)
        poster.resolve_stale(existing, to_post, self.dry_run)
        if self.dry_run:
            return 0
        gate_outcome = QualityGate(provider, self.owner, self.repo, self.pr_number, cfg).evaluate(
            to_post
        )
        if gate_outcome is not None:
            runner_mod._log_quality_gate_review_outcome("Full-review", gate_outcome)
        else:
            logger.warning(
                "Full-review: skipping PR-level quality gate summary/decision because "
                "unresolved review-item lookup failed."
            )
        count = 0
        if to_post:
            if not self.head_sha:
                raise ValueError(
                    "head_sha is required when posting comments (dry_run=False). "
                    "Provide head_sha or use --dry-run to skip posting."
                )
            count = poster.post_inline(
                incremental_base_sha,
                to_post,
                cfg,
                llm_cfg,
                full_diff=full_diff,
            )
        if (
            gate_outcome is not None
            and self.head_sha
            and provider.capabilities().omit_fingerprint_marker_in_body
        ):
            planned = len(to_post)
            include_marker = planned == 0 or count == planned
            poster.post_omit_marker_summary(
                cfg,
                llm_cfg,
                incremental_base_sha,
                findings_planned=planned,
                successful_inline_posts=count,
                gate_outcome=gate_outcome,
                include_run_marker=include_marker,
            )
        runner_mod._maybe_submit_review_decision(
            provider,
            self.owner,
            self.repo,
            self.pr_number,
            self.head_sha,
            self.dry_run,
            cfg,
            gate_outcome=gate_outcome,
        )
        return count

    @staticmethod
    def print_findings_summary(
        print_findings: bool, to_post: list[tuple[FindingV1, str]]
    ) -> None:
        if not print_findings:
            return
        if to_post:
            print("\n" + "=" * 60)
            print(f"Code Review Findings ({len(to_post)} total)")
            print("=" * 60)
            for f, _ in to_post:
                line_info = (
                    f"{f.line}"
                    if not f.end_line or f.end_line == f.line
                    else f"{f.line}-{f.end_line}"
                )
                print(f"[{f.severity.upper()}] {f.path}:{line_info}")
                print(f"Message: {f.get_body()}")
                if f.suggested_patch:
                    print("-" * 40)
                    print("Suggested Patch:")
                    print(f.suggested_patch)
                print("=" * 60)
            print()
        else:
            print("No findings to post.")

    @staticmethod
    def log_post_counts(dry_run: bool, planned_count: int, successful_post_count: int) -> None:
        if dry_run:
            logger.info("Dry run: would post %d comment(s)", planned_count)
        else:
            logger.info("Posted %d comment(s)", successful_post_count)

    @staticmethod
    def filter_findings_to_pr_paths(
        findings: list[FindingV1], paths: list[str]
    ) -> list[FindingV1]:
        if not paths:
            return findings
        allowed_normalized = {runner_mod._normalize_path_for_anchor(p) for p in paths}
        filtered_findings: list[FindingV1] = []
        for finding in findings:
            norm_path = runner_mod._normalize_path_for_anchor(finding.path or "")
            if norm_path in allowed_normalized:
                filtered_findings.append(finding)
            elif logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Dropping finding for path not in diff: %s (normalized=%s, allowed=%s)",
                    finding.path,
                    norm_path,
                    sorted(allowed_normalized),
                )
        return filtered_findings

    @staticmethod
    def filter_findings_to_visible_diff_lines(
        findings: list[FindingV1], full_diff: str
    ) -> list[FindingV1]:
        if not full_diff:
            return findings
        visible_lines = runner_mod._diff_visible_new_lines(full_diff)
        if not visible_lines:
            return findings
        line_filtered: list[FindingV1] = []
        for finding in findings:
            norm_path = runner_mod._normalize_path_for_anchor(finding.path or "")
            if (norm_path, finding.line) in visible_lines:
                line_filtered.append(finding)
            else:
                logger.debug(
                    "Dropping finding for line not visible in diff: %s:%d",
                    finding.path,
                    finding.line,
                )
        return line_filtered

    @staticmethod
    def filter_findings_to_added_diff_lines(
        findings: list[FindingV1], full_diff: str
    ) -> list[FindingV1]:
        if not full_diff:
            return findings
        added_lines = runner_mod._added_lines_in_diff(full_diff)
        if not added_lines:
            return []
        line_filtered: list[FindingV1] = []
        for finding in findings:
            norm_path = runner_mod._normalize_path_for_anchor(finding.path or "")
            if (norm_path, finding.line) in added_lines:
                line_filtered.append(finding)
            else:
                logger.debug(
                    "Dropping finding for line not changed in diff: %s:%d",
                    finding.path,
                    finding.line,
                )
        return line_filtered

    def filter_findings_by_diff_scope(
        self,
        findings: list[FindingV1],
        paths: list[str],
        full_diff: str,
        *,
        review_visible_lines: bool = False,
    ) -> list[FindingV1]:
        path_filtered = self.filter_findings_to_pr_paths(findings, paths)
        pipeline_results = FindingRefinementPipeline().run(path_filtered, full_diff)
        if review_visible_lines:
            return self.filter_findings_to_visible_diff_lines(pipeline_results, full_diff)
        return self.filter_findings_to_added_diff_lines(pipeline_results, full_diff)

    def validate_context_sources_or_raise(
        self,
        ctx_cfg,
        cfg,
        run_observability: ReviewRunObservability,
    ) -> None:
        """Validate context-aware sources when enabled and finish observability on fatal config."""
        try:
            self.context_enricher.validate_context_sources_or_raise(ctx_cfg, cfg)
        except runner_mod.ContextAwareFatalError as e:
            logger.error("Context-aware review configuration error: %s", e)
            run_observability.finish(self.pr_ctx, [], [], [])
            raise

    def _check_early_exits(
        self,
        provider: ProviderInterface,
        cfg: Any,
        llm_cfg: Any,
        run_observability: ReviewRunObservability,
        skip_if_needed: Callable[..., list[FindingV1] | None],
        compute_idempotency_and_maybe_short_circuit: Callable[..., list[FindingV1] | None],
        incremental_base_sha_fn: Callable[[Any, str], str],
        comment_mgr: CommentManager,
    ) -> tuple[list[FindingV1] | None, str]:
        """Check for skip-status or idempotency short-circuits."""
        skip_result = skip_if_needed(provider, cfg, run_observability)
        if skip_result is not None:
            return skip_result, ""

        comment_mgr.load_existing_comments(provider, self.owner, self.repo, self.pr_number)
        existing_dicts = [c.model_dump() for c in comment_mgr.existing_comments]
        incremental_base_sha = incremental_base_sha_fn(cfg, self.head_sha)

        idempotency_result = compute_idempotency_and_maybe_short_circuit(
            cfg,
            llm_cfg,
            existing_dicts,
            run_observability,
            incremental_base_sha=incremental_base_sha,
        )
        if idempotency_result is not None:
            logger.info("Skipping run (idempotent: same review range/config already reviewed)")
            return idempotency_result, incremental_base_sha

        return None, incremental_base_sha

    @dataclass
    class _ReviewEnv:
        files: list[FileInfo]
        paths: list[str]
        full_diff: str
        incremental_base_sha: str
        pr_info: PRInfo | None
        early_exit_result: list[FindingV1] | None = None

    def _setup_review_environment(
        self,
        provider: ProviderInterface,
        cfg: Any,
        app_cfg: Any,
        run_observability: ReviewRunObservability,
        incremental_base_sha_fn: Callable[[Any, str], str],
    ) -> _ReviewEnv:
        """Fetch files, diffs, and PR metadata; handle empty scope."""
        ctx_cfg = runner_mod.get_context_aware_config()
        self.validate_context_sources_or_raise(ctx_cfg, cfg, run_observability)

        pr_info = provider.get_pr_info(self.owner, self.repo, self.pr_number)
        files, paths, full_diff, incremental_base_sha = self.fetch_review_files_and_diffs(
            provider,
            cfg,
            incremental_base_sha_fn=incremental_base_sha_fn,
        )
        self.log_review_scope_fetch(incremental_base_sha, self.head_sha, paths)

        empty_scope_result = self.review_decision_handler.maybe_finish_empty_scope_review(
            provider, cfg, self.head_sha, run_observability, paths, pr_info
        )
        if empty_scope_result is not None:
            return self._ReviewEnv([], [], "", "", None, early_exit_result=empty_scope_result)

        started_notice_already_posted = bool(
            getattr(app_cfg, "started_review_comment_posted", False)
        )
        if not self.dry_run and not started_notice_already_posted:
            CommentPoster(provider, self.pr_ctx).post_started_review_comment(pr_info)

        return self._ReviewEnv(files, paths, full_diff, incremental_base_sha, pr_info)

    @dataclass
    class _ReviewExecution:
        all_findings: list[FindingV1]
        context_brief_attached: bool
        prompt_suffix: str
        early_exit_result: list[FindingV1] | None = None

    def _execute_review_agent(
        self,
        provider: ProviderInterface,
        cfg: Any,
        app_cfg: Any,
        run_observability: ReviewRunObservability,
        env: _ReviewEnv,
        review_standards: Any,
    ) -> _ReviewExecution:
        """Handle batching, prompt enrichment, and running the review agent."""
        context_window = runner_mod.get_context_window()
        batch_budget = build_review_batch_budget(
            context_window_tokens=context_window,
            max_output_tokens=runner_mod.get_max_output_tokens(),
            diff_budget_ratio=runner_mod.DIFF_TOKEN_BUDGET_RATIO,
        )
        diff_budget = batch_budget.effective_diff_budget_tokens
        remaining_prompt_tokens = batch_budget.prompt_budget_tokens

        ctx_cfg = runner_mod.get_context_aware_config()
        refs, context_brief, prompt_suffix = self.context_enricher.build_prompt_suffix(
            provider, cfg, ctx_cfg, app_cfg, env.pr_info, env.full_diff, remaining_prompt_tokens
        )
        self.log_context_aware_prompt_inputs(refs, context_brief)

        batches = execution_mod.build_review_batches_for_scope(
            env.files, env.paths, env.full_diff, diff_budget
        )
        context_brief_attached = bool(context_brief and _CONTEXT_TAG in prompt_suffix)
        execution_mod.log_review_batch_plan(batches, env.paths, env.incremental_base_sha)

        if not batches:
            logger.info("Prepared zero review batches from the scoped diff; skipping LLM run")
            return self._ReviewExecution(
                [],
                context_brief_attached,
                prompt_suffix,
                early_exit_result=self._result_builder(
                    run_observability,
                    env.paths,
                    [],
                    0,
                    [],
                    context_brief_attached=context_brief_attached,
                ),
            )

        review_visible_lines = bool(getattr(app_cfg, "review_visible_lines", False))
        session_id, _session_service, runner = execution_mod.create_agent_and_runner(
            self.pr_ctx,
            provider,
            review_standards,
            batches,
            context_brief_attached=context_brief_attached,
            review_visible_lines=review_visible_lines,
        )
        all_findings = execution_mod.run_agent_and_collect_findings(
            self.pr_ctx,
            provider,
            review_standards,
            runner,
            session_id,
            batches,
            context_brief_attached=context_brief_attached,
            prompt_suffix=prompt_suffix,
            review_visible_lines=review_visible_lines,
        )
        return self._ReviewExecution(all_findings, context_brief_attached, prompt_suffix)

    def _refine_findings_funnel(
        self,
        provider: ProviderInterface,
        app_cfg: Any,
        env: _ReviewEnv,
        comment_mgr: CommentManager,
        all_findings: list[FindingV1],
    ) -> list[tuple[FindingV1, bool]]:
        """Filter by scope, deduplicate, and verify findings."""
        llm_returned_count = len(all_findings)
        review_visible_lines = bool(getattr(app_cfg, "review_visible_lines", False))

        findings = self.filter_findings_by_diff_scope(
            all_findings, env.paths, env.full_diff, review_visible_lines=review_visible_lines
        )
        after_scope_count = len(findings)

        to_post = comment_mgr.filter_duplicates(
            findings,
            self.make_fingerprint_fn(provider),
            use_collapsible_prompt=provider.capabilities().markup_supports_collapsible,
        )
        after_unique_count = len(to_post)

        try:
            from code_review.agent.verification_agent import verify_findings

            to_verify = [f for f, _ in to_post]
            verified_findings = verify_findings(to_verify, env.full_diff)
            verified_set = set(verified_findings)
            to_post = [item for item in to_post if item[0] in verified_set]
        except Exception as exc:
            logger.warning("Verification agent step failed; proceeding without it: %s", exc)

        after_verification_count = len(to_post)
        logger.info(
            "Funnel: LLM=%d → Scoped=%d → Unique=%d → Verified=%d",
            llm_returned_count,
            after_scope_count,
            after_unique_count,
            after_verification_count,
        )
        return to_post

    def _maybe_generate_and_post_summary(
        self,
        provider: ProviderInterface,
        env: _ReviewEnv,
        to_post: list[tuple[FindingV1, bool]],
    ) -> None:
        """Generate and post a PR summary; update PR description for initial reviews without one."""
        if self.dry_run:
            logger.info("Dry run: skipping LLM-generated PR summary")
            return

        description_was_empty = not (getattr(env.pr_info, "description", "") or "").strip()
        is_initial_review = not env.incremental_base_sha



        try:
            summary_text = self._generate_summary_text(provider, env, to_post)
            self._post_summary(
                provider, env, summary_text, description_was_empty, is_initial_review, to_post
            )
        except Exception as e:
            logger.warning("Failed to generate/post PR summary: %s", e, exc_info=True)

    def _generate_summary_text(
        self,
        provider: ProviderInterface,
        env: _ReviewEnv,
        to_post: list[tuple[FindingV1, bool]],
    ) -> str:
        """Generate the PR summary text using the summary agent."""
        from code_review.agent.summary_agent import create_summary_agent, generate_pr_summary

        incremental_commits: list[str] | None = None
        if env.incremental_base_sha and self.head_sha:
            try:
                incremental_commits = provider.get_incremental_pr_commit_messages(
                    self.owner, self.repo, self.pr_number, env.incremental_base_sha, self.head_sha
                )
            except Exception as e:
                logger.warning("Failed to fetch incremental commit messages: %s", e)

        pr_info_for_summary = env.pr_info
        if (
            env.incremental_base_sha
            and pr_info_for_summary
            and hasattr(pr_info_for_summary, "model_copy")
        ):
            # For incremental reviews, scrub the full PR description to avoid summary-drift.
            pr_info_for_summary = pr_info_for_summary.model_copy(update={"description": ""})

        posted_findings = [f for f, _ in to_post]
        logger.info(
            "Generating PR summary via LLM for %d changed file(s), %d posted finding(s)",
            len(env.paths),
            len(posted_findings),
        )
        summary_agent = create_summary_agent()
        return generate_pr_summary(
            summary_agent,
            pr_info_for_summary,
            posted_findings,
            env.paths,
            incremental_base_sha=env.incremental_base_sha,
            incremental_commits=incremental_commits,
        )

    def _post_summary(
        self,
        provider: ProviderInterface,
        env: _ReviewEnv,
        summary_text: str,
        description_was_empty: bool,
        is_initial_review: bool,
        to_post: list[tuple[FindingV1, bool]],
    ) -> None:
        """Post the generated summary text to the PR."""
        from code_review.agent.summary_agent import split_summary_for_pr_description

        poster = CommentPoster(provider, self.pr_ctx)

        if description_was_empty and is_initial_review:
            # Split: Summary+Description → PR description field.
            # Walkthrough+rest → comment (only when there are findings to discuss).
            description_part, comment_part = split_summary_for_pr_description(summary_text)
            if description_part:
                # Re-fetch the current PR state to guard against a race where the
                # author added a description while the review was running.
                current_pr_info = provider.get_pr_info(self.owner, self.repo, self.pr_number)
                current_description = (getattr(current_pr_info, "description", "") or "").strip()
                if current_description:
                    logger.info(
                        "PR description was updated while review ran; skipping overwrite "
                        "owner=%s repo=%s pr=%s",
                        self.owner, self.repo, self.pr_number,
                    )
                else:
                    logger.info(
                        "Updating PR description with LLM-generated summary "
                        "owner=%s repo=%s pr=%s",
                        self.owner, self.repo, self.pr_number,
                    )
                    poster.update_pr_description(description_part)
            if comment_part:
                poster.post_pr_summary(comment_part)
        else:
            # Incremental review or PR already had a description: post full summary as comment.
            poster.post_pr_summary(summary_text)

    @staticmethod
    def log_review_scope_fetch(incremental_base_sha: str, head_sha: str, paths: list[str]) -> None:
        """Emit a concise log line describing the fetched review scope."""
        if incremental_base_sha:
            logger.info(
                "Fetched incremental diff base=%s head=%s, %d file(s) to review",
                incremental_base_sha[:12],
                (head_sha or "")[:12],
                len(paths),
            )
            return
        logger.info("Fetched diff, %d file(s) to review", len(paths))

    @staticmethod
    def log_context_aware_prompt_inputs(refs: list[Any], context_brief: str) -> None:
        """Log context-aware prompt supplements only when present."""
        if refs:
            logger.info("context_aware: extracted %d reference(s) from PR text", len(refs))
        if context_brief:
            logger.info("context_aware: distilled context attached to review prompt")

    def run(
        self,
        run_observability: ReviewRunObservability,
        cfg,
        llm_cfg,
        provider,
        app_cfg,
        *,
        skip_if_needed: Callable[..., list[FindingV1] | None],
        compute_idempotency_and_maybe_short_circuit: Callable[..., list[FindingV1] | None],
        incremental_base_sha_fn: Callable[[Any, str], str],
    ) -> list[FindingV1]:
        """Execute the full non-decision-only review path."""
        pr_url = self.pr_ctx.pr_url(cfg)
        logger.info(
            "Reviewing %s/%s PR %s (provider=%s) URL: %s",
            self.owner,
            self.repo,
            self.pr_number,
            cfg.provider,
            pr_url,
        )
        print(f"Starting review for PR: {pr_url}")

        comment_mgr = CommentManager()
        early_exit_result, _ = self._check_early_exits(
            provider,
            cfg,
            llm_cfg,
            run_observability,
            skip_if_needed,
            compute_idempotency_and_maybe_short_circuit,
            incremental_base_sha_fn,
            comment_mgr,
        )
        if early_exit_result is not None:
            return early_exit_result

        env = self._setup_review_environment(
            provider, cfg, app_cfg, run_observability, incremental_base_sha_fn
        )
        if env.early_exit_result is not None:
            return env.early_exit_result

        _, review_standards = self.detect_languages_for_files(env.paths)
        execution = self._execute_review_agent(
            provider, cfg, app_cfg, run_observability, env, review_standards
        )
        if execution.early_exit_result is not None:
            return execution.early_exit_result

        to_post = self._refine_findings_funnel(
            provider, app_cfg, env, comment_mgr, execution.all_findings
        )

        self.print_findings_summary(self.print_findings, to_post)
        successful_post_count = self.post_findings_and_summary(
            provider,
            env.incremental_base_sha,
            to_post,
            cfg,
            llm_cfg,
            comment_mgr.existing_comments,
            full_diff=env.full_diff,
        )

        self._maybe_generate_and_post_summary(provider, env, to_post)

        self.log_post_counts(self.dry_run, len(to_post), successful_post_count)
        return self._result_builder(
            run_observability,
            env.paths,
            execution.all_findings,
            successful_post_count,
            to_post,
            context_brief_attached=execution.context_brief_attached,
        )
