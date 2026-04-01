from __future__ import annotations

import logging
import uuid

from code_review import __version__, observability
from code_review import orchestration_deps as runner_mod
from code_review.models import PRContext
from code_review.orchestration.context_enricher import ContextEnricher
from code_review.orchestration.filter import ReviewFilter
from code_review.orchestration.idempotency import _idempotency_key_seen_in_comments
from code_review.orchestration.reply_dismissal import ReplyDismissalHandler
from code_review.orchestration.review_decision import ReviewDecisionHandler
from code_review.orchestration.runner_utils import (
    ReviewRunObservability,
    _log_run_complete,
    _run_reply_dismissal_llm,
)
from code_review.orchestration.standard_review import StandardReviewHandler
from code_review.schemas.findings import FindingV1
from code_review.schemas.review_decision_event import ReviewDecisionEventContext

logger = logging.getLogger(__name__)


class ReviewOrchestrator:
    """Thin coordinator for a single code review run."""

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
        """Load SCM/LLM config and create the provider instance."""
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
        run_observability: ReviewRunObservability,
    ) -> list[FindingV1] | None:
        """Emit observability and return [] if skip config matches, else None."""
        if not cfg.skip_label and not cfg.skip_title_pattern:
            return None
        pr_info = provider.get_pr_info(self.owner, self.repo, self.pr_number)
        skip_reason = ReviewFilter().should_skip(pr_info, cfg)
        if skip_reason is None:
            return None
        run_observability.finish(self.pr_ctx, [], [], [])
        return []

    def _compute_idempotency_and_maybe_short_circuit(
        self,
        cfg,
        llm_cfg,
        existing_dicts: list,
        run_observability: ReviewRunObservability,
        incremental_base_sha: str = "",
    ) -> list[FindingV1] | None:
        """Short-circuit when this PR/range/config was already reviewed."""
        if runner_mod.get_code_review_app_config().disable_idempotency:
            return None
        if not self.head_sha:
            return None
        incremental_base_sha = incremental_base_sha or self._incremental_base_sha(
            cfg, self.head_sha
        )
        run_id = self.pr_ctx.idempotency_key(cfg, llm_cfg, incremental_base_sha)
        if not _idempotency_key_seen_in_comments(existing_dicts, run_id):
            return None
        run_observability.finish(self.pr_ctx, [], [], [])
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

    def _record_observability_and_build_result(
        self,
        run_observability: ReviewRunObservability,
        paths: list,
        all_findings: list[FindingV1],
        successful_post_count: int,
        to_post: list[tuple[FindingV1, str]],
        context_brief_attached: bool = False,
    ) -> list[FindingV1]:
        """Emit run_complete log + observability.finish_run, then return posted findings."""
        run_observability.finish(
            self.pr_ctx,
            paths,
            all_findings,
            successful_post_count,
            context_brief_attached=context_brief_attached,
        )
        return [f for f, _ in to_post]

    def _build_handlers(self) -> tuple[ReviewDecisionHandler, StandardReviewHandler]:
        context_enricher = ContextEnricher(self.pr_ctx)
        reply_dismissal_handler = ReplyDismissalHandler(
            self.pr_ctx,
            dry_run=self.dry_run,
            event_context=self._event_context,
            run_reply_dismissal_llm=_run_reply_dismissal_llm,
        )
        review_decision_handler = ReviewDecisionHandler(
            self.pr_ctx,
            dry_run=self.dry_run,
            event_context=self._event_context,
            reply_dismissal_handler=reply_dismissal_handler,
            result_builder=self._record_observability_and_build_result,
            skip_if_needed=self._skip_if_needed,
        )
        standard_review_handler = StandardReviewHandler(
            self.pr_ctx,
            dry_run=self.dry_run,
            print_findings=self.print_findings,
            context_enricher=context_enricher,
            review_decision_handler=review_decision_handler,
            result_builder=self._record_observability_and_build_result,
        )
        return review_decision_handler, standard_review_handler

    def run(self) -> list[FindingV1]:
        """Execute the full review flow and return the findings that were posted."""
        trace_id = str(uuid.uuid4())
        logger.info(
            "run_start agent_version=%s trace_id=%s pr=%s/%s#%s",
            __version__,
            trace_id,
            self.owner,
            self.repo,
            self.pr_number,
        )
        run_handle = observability.start_run(trace_id)
        run_observability = ReviewRunObservability(
            trace_id,
            run_handle,
            log_run_complete=_log_run_complete,
            finish_run=observability.finish_run,
        )
        cfg, llm_cfg, provider = self._load_config_and_provider()
        app_cfg = runner_mod.get_code_review_app_config()
        review_decision_handler, standard_review_handler = self._build_handlers()
        decision_only = bool(self._review_decision_only) or bool(app_cfg.review_decision_only)
        if decision_only:
            return review_decision_handler.run_review_decision_only(
                run_observability, cfg, provider
            )
        return standard_review_handler.run(
            run_observability,
            cfg,
            llm_cfg,
            provider,
            app_cfg,
            skip_if_needed=self._skip_if_needed,
            compute_idempotency_and_maybe_short_circuit=(
                self._compute_idempotency_and_maybe_short_circuit
            ),
            incremental_base_sha_fn=self._incremental_base_sha,
        )
