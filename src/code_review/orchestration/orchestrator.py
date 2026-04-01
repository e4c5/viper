from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from code_review import observability
from code_review import orchestration_deps as runner_mod
from code_review.batching import ReviewBatch, build_review_batch_budget
from code_review.comments.manager import CommentManager
from code_review.models import PRContext
from code_review.orchestration import execution as execution_mod
from code_review.orchestration.events import (
    ReplyDismissalContext,
    _reply_added_event_authored_by_bot,
    _reply_dismissal_diff_context_for_thread,
    _reply_dismissal_scm_already_addressed_reason,
)
from code_review.orchestration.filter import ReviewFilter
from code_review.orchestration.idempotency import _idempotency_key_seen_in_comments
from code_review.orchestration.posting import CommentPoster
from code_review.orchestration.runner_utils import _log_run_complete, _run_reply_dismissal_llm
from code_review.providers.base import BotAttributionIdentity, RateLimitError
from code_review.quality.gate import QualityGate
from code_review.refinement.pipeline import FindingRefinementPipeline
from code_review.schemas.findings import FindingV1
from code_review.schemas.reply_dismissal import ReplyDismissalVerdictV1
from code_review.schemas.review_decision_event import ReviewDecisionEventContext
from code_review.schemas.review_thread_dismissal import ReviewThreadDismissalContext

logger = logging.getLogger(__name__)

_CONTEXT_TAG = "<context>"


class ReviewOrchestrator:
    """Orchestrates a single code review run (findings-only mode)."""

    def __init__(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str = "",
        *,
        dry_run: bool = False,
        print_findings: bool = False,
        review_decision_enabled: bool | None = None,
        review_decision_high_threshold: int | None = None,
        review_decision_medium_threshold: int | None = None,
        review_decision_only: bool = False,
        event_context: ReviewDecisionEventContext | None = None,
    ):
        self.pr_ctx = PRContext(owner, repo, pr_number, head_sha)
        self.dry_run = dry_run
        self.print_findings = print_findings
        self._review_decision_enabled_override = review_decision_enabled
        self._review_decision_high_threshold_override = review_decision_high_threshold
        self._review_decision_medium_threshold_override = review_decision_medium_threshold
        self._review_decision_only = review_decision_only
        self._event_context = event_context

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

    def _load_config_and_provider(self):
        """Load SCM/LLM config and create the provider instance.

        Returns (cfg, llm_cfg, provider).
        """
        cfg = runner_mod.get_scm_config()
        overrides: dict[str, bool | int] = {}
        if self._review_decision_enabled_override is not None:
            overrides["review_decision_enabled"] = self._review_decision_enabled_override
        if self._review_decision_high_threshold_override is not None:
            overrides["review_decision_high_threshold"] = (
                self._review_decision_high_threshold_override
            )
        if self._review_decision_medium_threshold_override is not None:
            overrides["review_decision_medium_threshold"] = (
                self._review_decision_medium_threshold_override
            )
        if overrides:
            cfg = cfg.model_copy(update=overrides)
        llm_cfg = runner_mod.get_llm_config()
        token_val = (
            cfg.token.get_secret_value() if hasattr(cfg.token, "get_secret_value") else cfg.token
        )
        provider = runner_mod.get_provider(
            cfg.provider,
            cfg.url,
            token_val,
            bitbucket_server_user_slug=cfg.bitbucket_server_user_slug,
        )
        return (cfg, llm_cfg, provider)

    def _skip_if_needed(
        self,
        provider,
        cfg,
        trace_id: str,
        start_time: float,
        run_handle,
    ) -> list[FindingV1] | None:
        """Delegate to ReviewFilter; emit observability and return [] if skip, else None."""
        if not cfg.skip_label and not cfg.skip_title_pattern:
            return None
        pr_info = provider.get_pr_info(self.owner, self.repo, self.pr_number)
        skip_reason = ReviewFilter().should_skip(pr_info, cfg)
        if skip_reason is None:
            return None
        _duration_ms = (time.perf_counter() - start_time) * 1000
        _log_run_complete(
            trace_id, self.owner, self.repo, self.pr_number, 0, 0, 0, _duration_ms
        )
        observability.finish_run(
            run_handle,
            self.owner,
            self.repo,
            self.pr_number,
            0,
            0,
            0,
            _duration_ms / 1000.0,
        )
        return []

    def _compute_idempotency_and_maybe_short_circuit(
        self,
        cfg,
        llm_cfg,
        existing_dicts: list,
        trace_id: str,
        start_time: float,
        run_handle,
        incremental_base_sha: str = "",
    ) -> list[FindingV1] | None:
        """
        If we already ran for this PR/range/config (run id in comment marker),
        emit observability and return []. Otherwise return None (caller continues).
        """
        if not self.head_sha:
            return None
        incremental_base_sha = incremental_base_sha or self._incremental_base_sha(
            cfg, self.head_sha
        )
        run_id = self.pr_ctx.idempotency_key(cfg, llm_cfg, incremental_base_sha)
        if not _idempotency_key_seen_in_comments(existing_dicts, run_id):
            return None
        _duration_ms = (time.perf_counter() - start_time) * 1000
        _log_run_complete(
            trace_id, self.owner, self.repo, self.pr_number, 0, 0, 0, _duration_ms
        )
        observability.finish_run(
            run_handle,
            self.owner,
            self.repo,
            self.pr_number,
            0,
            0,
            0,
            _duration_ms / 1000.0,
        )
        return []

    @staticmethod
    def _incremental_base_sha(cfg, head_sha: str) -> str:
        """Return a usable incremental review base SHA, else ``""`` for full-PR review."""
        raw_base_sha = getattr(cfg, "base_sha", "")
        base_sha = raw_base_sha.strip() if isinstance(raw_base_sha, str) else ""
        raw_head_sha = head_sha if head_sha else getattr(cfg, "head_sha", "")
        head_sha = raw_head_sha.strip() if isinstance(raw_head_sha, str) else ""
        if not base_sha or not head_sha or base_sha == head_sha:
            return ""
        return base_sha

    def _fetch_review_files_and_diffs(
        self, provider, cfg
    ) -> tuple[list[object], list[str], str, str]:
        """Fetch the file list and diff for the active review scope.

        Returns ``(files, paths, full_diff, incremental_base_sha)`` where
        ``incremental_base_sha`` is non-empty only when the review was scoped to
        ``SCM_BASE_SHA..SCM_HEAD_SHA``.
        """
        base_sha = self._incremental_base_sha(cfg, self.head_sha)
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

    def _detect_languages_for_files(self, paths: list[str]):
        """Run language detection on paths and return (detected, review_standards)."""
        detected = runner_mod.detect_from_paths(paths)
        review_standards = runner_mod.get_review_standards(detected.language, detected.framework)
        return (detected, review_standards)

    def _create_agent_and_runner(
        self,
        provider,
        review_standards: str,
        batches: list[ReviewBatch],
        *,
        context_brief_attached: bool = False,
    ):
        """
        Build the batch-review SequentialAgent, session service, and ADK Runner.
        Returns (session_id, session_service, runner).
        """
        return execution_mod.create_agent_and_runner(
            self.pr_ctx,
            provider,
            review_standards,
            batches,
            context_brief_attached=context_brief_attached,
        )

    def _run_agent_and_collect_findings(
        self,
        provider,
        review_standards: str,
        runner,
        session_id: str,
        batches: list[ReviewBatch],
        *,
        context_brief_attached: bool = False,
        prompt_suffix: str = "",
    ) -> list[FindingV1]:
        """
        Run the batch-review agent and parse responses into FindingV1 list.
        Returns all_findings (unfiltered).
        """
        return execution_mod.run_agent_and_collect_findings(
            self.pr_ctx,
            provider,
            review_standards,
            runner,
            session_id,
            batches,
            context_brief_attached=context_brief_attached,
            prompt_suffix=prompt_suffix,
        )

    def _run_sequential_batch_review_mode(
        self,
        provider,
        review_standards: str,
        runner,
        session_id: str,
        *,
        batches: list[ReviewBatch],
        batch_count: int,
        context_brief_attached: bool = False,
        prompt_suffix: str = "",
    ) -> list[FindingV1]:
        """Run the SequentialAgent batch workflow and preserve successful batches on rate limit."""
        return execution_mod._run_sequential_batch_review_mode(
            self.pr_ctx,
            provider,
            review_standards,
            runner,
            session_id,
            batches=batches,
            batch_count=batch_count,
            context_brief_attached=context_brief_attached,
            prompt_suffix=prompt_suffix,
        )

    def _build_batch_review_content(
        self,
        *,
        batch_count: int,
        prompt_suffix: str = "",
    ):
        """Build the user message used to execute a prepared batch-review workflow."""
        return execution_mod.build_batch_review_content(
            pr_ctx=self.pr_ctx,
            batch_count=batch_count,
            prompt_suffix=prompt_suffix,
        )

    @staticmethod
    def _findings_from_batch_responses(
        responses: list[tuple[str, str]],
    ) -> list[FindingV1]:
        """Parse structured findings from a list of batch response texts."""
        return execution_mod.findings_from_batch_responses(responses)

    @staticmethod
    def _batch_index_from_author(author: str) -> int | None:
        """Extract the original batch index from a workflow response author name."""
        return execution_mod.batch_index_from_author(author)

    def _recover_rate_limited_batches(
        self,
        provider,
        review_standards: str,
        batches: list[ReviewBatch],
        *,
        completed_responses: list[tuple[str, str]],
        context_brief_attached: bool,
        prompt_suffix: str,
        error: RateLimitError,
    ) -> list[FindingV1]:
        """Keep successful batch responses and isolate the remaining batches one-by-one."""
        return execution_mod._recover_rate_limited_batches(
            self.pr_ctx,
            provider,
            review_standards,
            batches,
            completed_responses=completed_responses,
            context_brief_attached=context_brief_attached,
            prompt_suffix=prompt_suffix,
            error=error,
        )

    @staticmethod
    def _build_review_batches(
        files: list[object], paths: list[str], full_diff: str, diff_budget: int
    ) -> list[ReviewBatch]:
        """Slice the scoped diff by file and pack the resulting segments into ordered batches."""
        return execution_mod.build_review_batches_for_scope(files, paths, full_diff, diff_budget)

    @staticmethod
    def _log_review_batch_plan(
        batches: list[ReviewBatch], paths: list[str], incremental_base_sha: str
    ) -> None:
        """Emit a concise log line describing the prepared review batches."""
        execution_mod.log_review_batch_plan(batches, paths, incremental_base_sha)

    def _make_fingerprint_fn(self, provider):
        """Return a fingerprint function (FindingV1 -> str) backed by live file content."""
        _cache: dict[str, list[str]] = {}

        def _fingerprint_fn(finding: FindingV1) -> str:
            if not self.head_sha:
                return ""
            if finding.path not in _cache:
                file_lines_by_path = runner_mod._get_file_lines_by_path(
                    provider,
                    self.owner,
                    self.repo,
                    self.head_sha,
                    [finding.path],
                )
                _cache[finding.path] = file_lines_by_path.get(finding.path, [])
            file_lines_by_path = {finding.path: _cache[finding.path]}
            return (
                runner_mod._fingerprint_for_finding(finding, file_lines_by_path)
                if file_lines_by_path
                else ""
            )

        return _fingerprint_fn

    def _post_findings_and_summary(
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
        full_diff: used to set line_type (ADDED vs CONTEXT) so Bitbucket
        Server anchors comments correctly.
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

    def _record_observability_and_build_result(
        self,
        trace_id: str,
        start_time: float,
        run_handle,
        paths: list,
        all_findings: list[FindingV1],
        successful_post_count: int,
        to_post: list[tuple[FindingV1, str]],
        context_brief_attached: bool = False,
    ) -> list[FindingV1]:
        """
        Emit run_complete log and observability.finish_run, then return the list of findings posted.
        """
        _duration_ms = (time.perf_counter() - start_time) * 1000
        _log_run_complete(
            trace_id,
            self.owner,
            self.repo,
            self.pr_number,
            files_count=len(paths),
            findings_count=len(all_findings),
            posts_count=successful_post_count,
            duration_ms=_duration_ms,
        )
        observability.finish_run(
            run_handle,
            self.owner,
            self.repo,
            self.pr_number,
            files_count=len(paths),
            findings_count=len(all_findings),
            posts_count=successful_post_count,
            duration_seconds=_duration_ms / 1000.0,
            context_brief_attached=context_brief_attached,
        )
        return [f for f, _ in to_post]

    @staticmethod
    def _print_findings_summary(
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
    def _log_post_counts(dry_run: bool, planned_count: int, successful_post_count: int) -> None:
        if dry_run:
            logger.info("Dry run: would post %d comment(s)", planned_count)
        else:
            logger.info("Posted %d comment(s)", successful_post_count)

    @staticmethod
    def _filter_findings_to_pr_paths(
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
    def _filter_findings_to_visible_diff_lines(
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

    def _filter_findings_by_diff_scope(
        self, findings: list[FindingV1], paths: list[str], full_diff: str
    ) -> list[FindingV1]:
        path_filtered = self._filter_findings_to_pr_paths(findings, paths)
        pipeline_results = FindingRefinementPipeline().run(path_filtered, full_diff)
        return self._filter_findings_to_visible_diff_lines(pipeline_results, full_diff)

    def _load_commit_messages(self, provider, need_commits: bool) -> list[str]:
        raw = (
            provider.get_pr_commit_messages(self.owner, self.repo, self.pr_number)
            if need_commits
            else []
        )
        return raw if isinstance(raw, list) else []

    def _build_prompt_suffix(
        self,
        provider,
        cfg,
        ctx_cfg,
        app_cfg,
        pr_info_for_metadata,
        full_diff: str,
        remaining_tokens: int,
    ) -> tuple[list[object], str | None, str]:
        need_commits = app_cfg.include_commit_messages_in_prompt or ctx_cfg.enabled
        commit_messages = self._load_commit_messages(provider, need_commits)
        pr_title = (pr_info_for_metadata.title if pr_info_for_metadata else "") or ""
        pr_desc = (pr_info_for_metadata.description if pr_info_for_metadata else "") or ""
        refs = (
            runner_mod.extract_context_references(
                cfg.provider,
                self.owner,
                self.repo,
                [pr_title, pr_desc, *commit_messages],
                extract_github=ctx_cfg.github_issues_enabled,
                extract_gitlab=ctx_cfg.gitlab_issues_enabled,
                extract_jira=ctx_cfg.jira_enabled,
                extract_confluence=ctx_cfg.confluence_enabled,
            )
            if ctx_cfg.enabled
            else []
        )
        context_brief: str | None = None
        if ctx_cfg.enabled and refs:
            try:
                context_brief = runner_mod.build_context_brief_for_pr(ctx_cfg, cfg, refs, full_diff)
            except runner_mod.ContextAwareFatalError:
                logger.exception("Context-aware fetch or distillation failed")
                raise
        prompt_suffix = runner_mod._format_review_prompt_supplement(
            context_brief=context_brief,
            commit_messages=commit_messages,
            include_commit_messages=app_cfg.include_commit_messages_in_prompt,
            remaining_tokens=remaining_tokens,
        )
        return (refs, context_brief, prompt_suffix)

    def _decision_only_try_skip_when_bot_not_blocking(
        self,
        provider,
        app_cfg,
        trace_id: str,
        start_time: float,
        run_handle,
    ) -> list[FindingV1] | None:
        if not (
            app_cfg.review_decision_only_skip_if_bot_not_blocking
            and runner_mod.event_allows_decision_only_skip_when_bot_not_blocking(
                self._event_context
            )
        ):
            return None
        caps = provider.capabilities()
        if not caps.supports_bot_blocking_state_query:
            return None
        if provider.get_bot_blocking_state(self.owner, self.repo, self.pr_number) != "NOT_BLOCKING":
            return None
        logger.info(
            "Review-decision-only: skipping quality gate "
            "(bot not blocking; "
            "CODE_REVIEW_REVIEW_DECISION_ONLY_SKIP_IF_BOT_NOT_BLOCKING=1, "
            "comment_id present)."
        )
        return self._record_observability_and_build_result(
            trace_id,
            start_time,
            run_handle,
            paths=[],
            all_findings=[],
            successful_post_count=0,
            to_post=[],
        )

    def _decision_only_try_skip_when_event_actor_is_bot(
        self,
        provider,
        trace_id: str,
        start_time: float,
        run_handle,
    ) -> list[FindingV1] | None:
        """Skip review-decision-only runs triggered by the bot's own comment activity."""
        ctx = self._event_context
        if ctx is None:
            return None
        actor_login = (ctx.actor_login or "").strip()
        actor_id = (ctx.actor_id or "").strip()
        if not actor_login and (not actor_id):
            return None
        caps = provider.capabilities()
        if not caps.supports_bot_attribution_identity_query:
            return None
        bot_id = provider.get_bot_attribution_identity(self.owner, self.repo, self.pr_number)
        if not _reply_added_event_authored_by_bot(ctx, bot_id):
            return None
        if (ctx.comment_id or "").strip():
            observability.record_reply_dismissal_outcome("skipped_bot_author")
        logger.info(
            "Review-decision-only: skipping bot-authored webhook event "
            "(actor_login=%r actor_id=%r comment_id=%r source=%r)",
            actor_login,
            actor_id,
            (ctx.comment_id or "").strip(),
            (ctx.source or "").strip(),
        )
        return self._record_observability_and_build_result(
            trace_id,
            start_time,
            run_handle,
            paths=[],
            all_findings=[],
            successful_post_count=0,
            to_post=[],
        )

    def _validate_context_sources_or_raise(
        self,
        ctx_cfg,
        cfg,
        run_handle,
        start_time: float,
    ) -> None:
        """Validate context-aware sources when enabled and finish observability on fatal config."""
        if not ctx_cfg.enabled:
            return
        try:
            runner_mod.validate_context_aware_sources(ctx_cfg, cfg)
        except runner_mod.ContextAwareFatalError as e:
            logger.error("Context-aware review configuration error: %s", e)
            observability.finish_run(
                run_handle,
                self.owner,
                self.repo,
                self.pr_number,
                files_count=0,
                findings_count=0,
                posts_count=0,
                duration_seconds=time.perf_counter() - start_time,
            )
            raise

    @staticmethod
    def _log_review_scope_fetch(incremental_base_sha: str, head_sha: str, paths: list[str]) -> None:
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

    def _decision_only_maybe_post_disagreed_thread_reply(
        self,
        provider,
        caps_rd,
        comment_id: str,
        verdict: ReplyDismissalVerdictV1,
    ) -> None:
        if not caps_rd.supports_review_thread_reply:
            logger.info(
                "Reply-dismissal disagreed: provider does not support thread replies"
            )
            return
        if self.dry_run:
            truncated = (verdict.reply_text or "")[:500]
            logger.info(
                "Dry-run: would post review-thread reply (truncated): %s", truncated
            )
            return
        try:
            provider.post_review_thread_reply(
                self.owner, self.repo, self.pr_number, comment_id, verdict.reply_text
            )
            logger.info(
                "Reply-dismissal disagreed: posted follow-up reply to comment_id=%s",
                comment_id,
            )
        except Exception as e:
            logger.warning("post_review_thread_reply failed: %s", e)

    def _decision_only_maybe_post_agreed_thread_reply(
        self,
        provider,
        caps_rd,
        comment_id: str,
    ) -> bool:
        if not caps_rd.supports_review_thread_reply:
            logger.info(
                "Reply-dismissal agreed: provider does not support thread replies; "
                "cannot persist accepted thread state"
            )
            return False
        if self.dry_run:
            logger.info(
                "Dry-run: would post durable accepted-thread reply: %s",
                runner_mod.REPLY_DISMISSAL_ACCEPTED_REPLY_TEXT,
            )
            return False
        try:
            provider.post_review_thread_reply(
                self.owner,
                self.repo,
                self.pr_number,
                comment_id,
                runner_mod.REPLY_DISMISSAL_ACCEPTED_REPLY_TEXT,
            )
            logger.info(
                "Reply-dismissal agreed: posted durable accepted-thread reply to comment_id=%s",
                comment_id,
            )
            return True
        except Exception as e:
            logger.warning("post agreed accepted-thread reply failed: %s", e)
            return False

    def _decision_only_maybe_resolve_agreed_thread(
        self,
        provider,
        caps_rd,
        comment_id: str,
        dctx: ReviewThreadDismissalContext,
    ) -> bool:
        if not caps_rd.supports_review_thread_resolution:
            return self._decision_only_maybe_post_agreed_thread_reply(provider, caps_rd, comment_id)
        if self.dry_run:
            logger.info(
                "Dry-run: would resolve review thread stable_id=%s thread_id=%s",
                dctx.gate_exclusion_stable_id,
                (dctx.thread_id or "").strip(),
            )
            return False
        try:
            provider.resolve_review_thread(
                self.owner, self.repo, self.pr_number, dctx, comment_id
            )
            logger.info(
                "Reply-dismissal agreed: resolved review thread stable_id=%s thread_id=%s",
                dctx.gate_exclusion_stable_id,
                (dctx.thread_id or "").strip(),
            )
            return True
        except Exception as e:
            logger.warning("resolve_review_thread failed: %s", e)
            return self._decision_only_maybe_post_agreed_thread_reply(provider, caps_rd, comment_id)

    def _reply_dismissal_comment_id_or_none(self, app_cfg) -> str | None:
        """Return event comment id when reply-dismissal should run, else ``None``."""
        ctx = self._event_context
        comment_id = (ctx.comment_id or "").strip() if ctx else ""
        if not app_cfg.reply_dismissal_enabled:
            if comment_id:
                logger.info(
                    "Reply-dismissal disabled: "
                    "CODE_REVIEW_REPLY_DISMISSAL_ENABLED is explicitly false; "
                    "skipping LLM for comment_id=%s",
                    comment_id,
                )
            return None
        if app_cfg.reply_dismissal_enabled and ctx is not None and (ctx.comment_id or "").strip():
            return ctx.comment_id.strip()
        logger.info(
            "Reply-dismissal enabled but not run: requires "
            "CODE_REVIEW_EVENT_COMMENT_ID; "
            "got comment_id=%r ctx_present=%s",
            comment_id or "",
            ctx is not None,
        )
        return None

    def _reply_dismissal_precheck(
        self, provider, comment_id: str
    ) -> tuple[BotAttributionIdentity, ReviewThreadDismissalContext] | None:
        """Return bot identity and dismissal context when reply-dismissal can proceed."""
        ctx = self._event_context
        if ctx is None:
            logger.info("Reply-dismissal skipped: no event context present")
            return None
        logger.info(
            "Reply-dismissal candidate: loading thread context for comment_id=%s",
            comment_id,
        )
        bot_id = provider.get_bot_attribution_identity(self.owner, self.repo, self.pr_number)
        if _reply_added_event_authored_by_bot(ctx, bot_id):
            observability.record_reply_dismissal_outcome("skipped_bot_author")
            logger.info(
                "Reply-dismissal skipped: reply_added actor matches bot "
                "(actor_login=%r actor_id=%r)",
                (ctx.actor_login or "").strip(),
                (ctx.actor_id or "").strip(),
            )
            return None
        caps_rd = provider.capabilities()
        if not caps_rd.supports_review_thread_dismissal_context:
            observability.record_reply_dismissal_outcome("skipped_no_capability")
            logger.info(
                "Reply-dismissal skipped: provider does not support review-thread context"
            )
            return None
        dctx = provider.get_review_thread_dismissal_context(
            self.owner, self.repo, self.pr_number, comment_id
        )
        if dctx is None or len(dctx.entries) < 2:
            observability.record_reply_dismissal_outcome("skipped_insufficient_thread")
            logger.info(
                "Reply-dismissal skipped: insufficient thread context "
                "for comment_id=%s (entries=%s)",
                comment_id,
                len(dctx.entries) if dctx is not None else 0,
            )
            return None
        existing_bot_reply = ReplyDismissalContext(dctx, bot_id).existing_bot_reply_after_trigger(
            comment_id
        )
        if existing_bot_reply is not None:
            observability.record_reply_dismissal_outcome("skipped_already_replied")
            logger.info(
                "Reply-dismissal skipped: triggering comment_id=%s already has "
                "a later bot reply in thread (comment_id=%s)",
                comment_id,
                (existing_bot_reply.comment_id or "").strip(),
            )
            return None
        logger.info(
            "Reply-dismissal thread loaded: comment_id=%s entries=%d stable_id=%s thread_id=%s",
            comment_id,
            len(dctx.entries),
            dctx.gate_exclusion_stable_id,
            (dctx.thread_id or "").strip(),
        )
        return (bot_id, dctx)

    @staticmethod
    def _reply_dismissal_parse_verdict(
        raw_verdict: str,
    ) -> ReplyDismissalVerdictV1 | None:
        """Parse reply-dismissal LLM output and log a helpful truncated warning on failure."""
        verdict = runner_mod.reply_dismissal_verdict_from_llm_text(raw_verdict)
        if verdict is not None:
            return verdict
        observability.record_reply_dismissal_outcome("parse_failed")
        snippet = (raw_verdict or "").strip()
        if len(snippet) > 1500:
            snippet = snippet[:1500] + "…"
        logger.warning(
            "Reply-dismissal LLM output could not be parsed as "
            "ReplyDismissalVerdictV1; enable DEBUG for full request/response. "
            "Raw (truncated): %r",
            snippet or "(empty)",
        )
        return None

    def _reply_dismissal_diff_context(
        self,
        provider,
        dctx: ReviewThreadDismissalContext,
    ) -> str:
        path = (dctx.path or "").strip()
        if not path:
            return ""
        if not provider.capabilities().supports_lightweight_pr_diff_for_file:
            return ""
        try:
            return _reply_dismissal_diff_context_for_thread(
                provider.get_pr_diff_for_file(self.owner, self.repo, self.pr_number, path), dctx
            )
        except Exception as e:
            logger.warning(
                "Reply-dismissal diff context unavailable for path=%s line=%s: %s",
                path,
                int(dctx.line or 0),
                e,
            )
            return ""

    def _reply_dismissal_run_llm_and_parse(
        self, user_msg: str
    ) -> ReplyDismissalVerdictV1 | None:
        try:
            raw_verdict = _run_reply_dismissal_llm(user_msg)
        except Exception as e:
            logger.warning("Reply-dismissal LLM run failed: %s", e)
            observability.record_reply_dismissal_outcome("llm_error")
            return None
        logger.info(
            "Reply-dismissal LLM completed: response_chars=%d",
            len((raw_verdict or "").strip()),
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Reply-dismissal raw LLM response: %s",
                runner_mod._reply_dismissal_response_log_snippet(raw_verdict, limit=4000),
            )
        return self._reply_dismissal_parse_verdict(raw_verdict)

    def _reply_dismissal_excluded_gate_ids_from_verdict(
        self,
        provider,
        comment_id: str,
        dctx: ReviewThreadDismissalContext,
        verdict: ReplyDismissalVerdictV1,
    ) -> frozenset[str]:
        if verdict.verdict == "agreed":
            observability.record_reply_dismissal_outcome("agreed")
            persisted = self._decision_only_maybe_resolve_agreed_thread(
                provider, provider.capabilities(), comment_id, dctx
            )
            if persisted:
                logger.info(
                    "Reply-dismissal agreed; excluding gate stable_id=%s",
                    dctx.gate_exclusion_stable_id,
                )
                return frozenset({dctx.gate_exclusion_stable_id})
            logger.info(
                "Reply-dismissal agreed but SCM persistence failed; "
                "keeping gate stable_id=%s in quality gate",
                dctx.gate_exclusion_stable_id,
            )
            return frozenset()
        if verdict.verdict == "disagreed":
            observability.record_reply_dismissal_outcome("disagreed")
            self._decision_only_maybe_post_disagreed_thread_reply(
                provider, provider.capabilities(), comment_id, verdict
            )
        return frozenset()

    def _decision_only_reply_dismissal_excluded_gate_ids(
        self,
        provider,
        app_cfg,
        trace_id: str,
    ) -> frozenset[str]:
        """Stable ids to exclude from the quality gate after optional reply-dismissal LLM."""
        comment_id = self._reply_dismissal_comment_id_or_none(app_cfg)
        if comment_id is None:
            return frozenset()
        precheck = self._reply_dismissal_precheck(provider, comment_id)
        if precheck is None:
            return frozenset()
        bot_id, dctx = precheck
        scm_reason = _reply_dismissal_scm_already_addressed_reason(dctx)
        if scm_reason:
            observability.record_reply_dismissal_outcome("skipped_scm_already_addressed")
            logger.info(
                "Reply-dismissal skipped LLM: SCM already indicates thread "
                "addressed (reason=%s stable_id=%s comment_id=%s)",
                scm_reason,
                dctx.gate_exclusion_stable_id,
                comment_id,
            )
            return frozenset({dctx.gate_exclusion_stable_id})
        diff_context = self._reply_dismissal_diff_context(provider, dctx)
        user_msg = ReplyDismissalContext(dctx, bot_id).format_user_message(comment_id, diff_context)
        logger.info(
            "Reply-dismissal sending thread to LLM: comment_id=%s "
            "entries=%d stable_id=%s path=%s line=%s diff_context=%s",
            comment_id,
            len(dctx.entries),
            dctx.gate_exclusion_stable_id,
            (dctx.path or "").strip(),
            int(dctx.line or 0),
            "yes" if diff_context else "no",
        )
        verdict = self._reply_dismissal_run_llm_and_parse(user_msg)
        if verdict is None:
            return frozenset()
        logger.info(
            "reply_dismissal_verdict trace_id=%s verdict=%s pr=%s/%s#%s",
            trace_id,
            verdict.verdict,
            self.owner,
            self.repo,
            self.pr_number,
        )
        return self._reply_dismissal_excluded_gate_ids_from_verdict(
            provider, comment_id, dctx, verdict
        )

    def _run_review_decision_only(
        self, trace_id: str, start_time: float, run_handle, cfg, provider
    ) -> list[FindingV1]:
        """Recompute quality-gate counts from SCM state and submit review decision only."""
        pr_url = self.pr_ctx.pr_url(cfg)
        logger.info(
            "Review-decision-only run for %s/%s PR %s (provider=%s) URL: %s",
            self.owner,
            self.repo,
            self.pr_number,
            cfg.provider,
            pr_url,
        )
        print(f"Review-decision-only for PR: {pr_url}")
        runner_mod._log_review_decision_event_if_present(self._event_context)
        app_cfg = runner_mod.get_code_review_app_config()

        skip_result = self._skip_if_needed(provider, cfg, trace_id, start_time, run_handle)
        if skip_result is not None:
            return skip_result
        skip_bot_event = self._decision_only_try_skip_when_event_actor_is_bot(
            provider, trace_id, start_time, run_handle
        )
        if skip_bot_event is not None:
            return skip_bot_event
        skip_early = self._decision_only_try_skip_when_bot_not_blocking(
            provider, app_cfg, trace_id, start_time, run_handle
        )
        if skip_early is not None:
            return skip_early
        head_hint = runner_mod._head_sha_hint_for_decision_only(self.head_sha)
        head_sha = runner_mod._resolve_head_sha_for_review_decision_submission(
            provider, self.owner, self.repo, self.pr_number, head_hint
        )
        if not head_sha and (not self.dry_run):
            logger.warning(
                "Review-decision-only: head_sha missing after provider lookup; "
                "submit_review_decision may omit commit id for some SCMs."
            )
        excluded_gate = self._decision_only_reply_dismissal_excluded_gate_ids(
            provider, app_cfg, trace_id
        )
        gate_outcome = QualityGate(
            provider, self.owner, self.repo, self.pr_number, cfg
        ).evaluate(
            [],
            excluded_gate_stable_ids=excluded_gate if excluded_gate else None,
        )
        if gate_outcome is not None:
            runner_mod._log_quality_gate_review_outcome("Review-decision-only", gate_outcome)
        else:
            logger.warning(
                "Review-decision-only: skipping PR review decision because unresolved "
                "review-item lookup failed."
            )
        runner_mod._maybe_submit_review_decision(
            provider,
            self.owner,
            self.repo,
            self.pr_number,
            head_sha,
            self.dry_run,
            cfg,
            gate_outcome=gate_outcome,
        )
        return self._record_observability_and_build_result(
            trace_id,
            start_time,
            run_handle,
            paths=[],
            all_findings=[],
            successful_post_count=0,
            to_post=[],
        )

    def _resolve_empty_scope_submission_head_sha(
        self,
        provider,
        head_sha: str,
        pr_info_for_metadata: Any,
    ) -> str:
        """Resolve the best head SHA to use when only refreshing the review decision."""
        if head_sha:
            return head_sha
        api_head_sha = (getattr(pr_info_for_metadata, "head_sha", None) or "").strip()
        if api_head_sha:
            return api_head_sha
        return runner_mod._resolve_head_sha_for_review_decision_submission(
            provider, self.owner, self.repo, self.pr_number, ""
        )

    def _maybe_finish_empty_scope_review(
        self,
        provider,
        cfg,
        head_sha: str,
        trace_id: str,
        start_time: float,
        run_handle,
        paths: list[str],
        pr_info_for_metadata: Any,
    ) -> list[FindingV1] | None:
        """Handle the empty-review-scope early return, including review-decision refresh."""
        if paths:
            return None
        logger.info("No files to review")
        if bool(getattr(cfg, "review_decision_enabled", False)):
            logger.info(
                "Recomputing PR review decision from unresolved SCM state "
                "despite empty review scope"
            )
            gate_outcome = QualityGate(
                provider, self.owner, self.repo, self.pr_number, cfg
            ).evaluate([])
            if gate_outcome is not None:
                runner_mod._log_quality_gate_review_outcome("Empty-scope refresh", gate_outcome)
            else:
                logger.warning(
                    "Empty-scope refresh: skipping PR review decision because unresolved "
                    "review-item lookup failed."
                )
            submission_head_sha = self._resolve_empty_scope_submission_head_sha(
                provider, head_sha, pr_info_for_metadata
            )
            runner_mod._maybe_submit_review_decision(
                provider,
                self.owner,
                self.repo,
                self.pr_number,
                submission_head_sha,
                self.dry_run,
                cfg,
                gate_outcome=gate_outcome,
            )
        return self._record_observability_and_build_result(
            trace_id, start_time, run_handle, paths, [], 0, []
        )

    def _log_context_aware_prompt_inputs(
        self, refs: list[Any], context_brief: str
    ) -> None:
        """Log context-aware prompt supplements only when present."""
        if refs:
            logger.info(
                "context_aware: extracted %d reference(s) from PR text", len(refs)
            )
        if context_brief:
            logger.info("context_aware: distilled context attached to review prompt")

    def _run_standard_review(
        self,
        trace_id: str,
        start_time: float,
        run_handle,
        cfg,
        llm_cfg,
        provider,
        app_cfg,
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
        skip_result = self._skip_if_needed(provider, cfg, trace_id, start_time, run_handle)
        if skip_result is not None:
            return skip_result
        comment_mgr = CommentManager()
        comment_mgr.load_existing_comments(provider, self.owner, self.repo, self.pr_number)
        existing = comment_mgr.existing_comments
        existing_dicts = [c.model_dump() for c in existing]
        incremental_base_sha = self._incremental_base_sha(cfg, self.head_sha)
        idempotency_result = self._compute_idempotency_and_maybe_short_circuit(
            cfg,
            llm_cfg,
            existing_dicts,
            trace_id,
            start_time,
            run_handle,
            incremental_base_sha=incremental_base_sha,
        )
        if idempotency_result is not None:
            logger.info(
                "Skipping run (idempotent: same review range/config already reviewed)"
            )
            return idempotency_result
        ctx_cfg = runner_mod.get_context_aware_config()
        self._validate_context_sources_or_raise(ctx_cfg, cfg, run_handle, start_time)
        pr_info_for_metadata = provider.get_pr_info(self.owner, self.repo, self.pr_number)
        files, paths, full_diff, incremental_base_sha = self._fetch_review_files_and_diffs(
            provider, cfg
        )
        self._log_review_scope_fetch(incremental_base_sha, self.head_sha, paths)
        empty_scope_result = self._maybe_finish_empty_scope_review(
            provider,
            cfg,
            self.head_sha,
            trace_id,
            start_time,
            run_handle,
            paths,
            pr_info_for_metadata,
        )
        if empty_scope_result is not None:
            return empty_scope_result
        if not self.dry_run:
            CommentPoster(provider, self.pr_ctx).post_started_review_comment(
                pr_info_for_metadata, paths
            )
        _, review_standards = self._detect_languages_for_files(paths)
        context_window = runner_mod.get_context_window()
        batch_budget = build_review_batch_budget(
            context_window_tokens=context_window,
            max_output_tokens=runner_mod.get_max_output_tokens(),
            diff_budget_ratio=runner_mod.DIFF_TOKEN_BUDGET_RATIO,
        )
        diff_budget = batch_budget.effective_diff_budget_tokens
        remaining_prompt_tokens = batch_budget.prompt_budget_tokens
        refs, context_brief, prompt_suffix = self._build_prompt_suffix(
            provider,
            cfg,
            ctx_cfg,
            app_cfg,
            pr_info_for_metadata,
            full_diff,
            remaining_prompt_tokens,
        )
        self._log_context_aware_prompt_inputs(refs, context_brief)
        batches = self._build_review_batches(
            files,
            paths,
            full_diff,
            diff_budget,
        )
        context_brief_attached = bool(context_brief and _CONTEXT_TAG in prompt_suffix)
        self._log_review_batch_plan(batches, paths, incremental_base_sha)
        if not batches:
            logger.info(
                "Prepared zero review batches from the scoped diff; skipping LLM run"
            )
            return self._record_observability_and_build_result(
                trace_id,
                start_time,
                run_handle,
                paths,
                [],
                0,
                [],
                context_brief_attached=context_brief_attached,
            )
        session_id, session_service, runner = self._create_agent_and_runner(
            provider,
            review_standards,
            batches,
            context_brief_attached=context_brief_attached,
        )
        all_findings = self._run_agent_and_collect_findings(
            provider,
            review_standards,
            runner,
            session_id,
            batches,
            context_brief_attached=context_brief_attached,
            prompt_suffix=prompt_suffix,
        )
        all_findings = self._filter_findings_by_diff_scope(all_findings, paths, full_diff)
        to_post = comment_mgr.filter_duplicates(
            all_findings,
            self._make_fingerprint_fn(provider),
            use_collapsible_prompt=provider.capabilities().markup_supports_collapsible,
        )
        logger.info(
            "Agent returned %d finding(s), %d to post after filtering",
            len(all_findings),
            len(to_post),
        )
        self._print_findings_summary(self.print_findings, to_post)
        successful_post_count = self._post_findings_and_summary(
            provider,
            incremental_base_sha,
            to_post,
            cfg,
            llm_cfg,
            existing,
            full_diff=full_diff,
        )
        self._log_post_counts(self.dry_run, len(to_post), successful_post_count)
        return self._record_observability_and_build_result(
            trace_id,
            start_time,
            run_handle,
            paths,
            all_findings,
            successful_post_count,
            to_post,
            context_brief_attached=context_brief_attached,
        )

    def run(self) -> list[FindingV1]:
        """
        Execute the full review flow. Returns list of findings that were posted
        (or would be posted if dry_run).
        """
        trace_id = str(uuid.uuid4())
        start_time = time.perf_counter()
        run_handle = observability.start_run(trace_id)
        cfg, llm_cfg, provider = self._load_config_and_provider()
        app_cfg = runner_mod.get_code_review_app_config()
        decision_only = bool(self._review_decision_only) or bool(app_cfg.review_decision_only)
        if decision_only:
            return self._run_review_decision_only(trace_id, start_time, run_handle, cfg, provider)
        return self._run_standard_review(
            trace_id, start_time, run_handle, cfg, llm_cfg, provider, app_cfg
        )
