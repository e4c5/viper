from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from code_review import observability
from code_review import orchestration_deps as runner_mod
from code_review.models import PRContext
from code_review.orchestration.events import _reply_added_event_authored_by_bot
from code_review.orchestration.reply_dismissal import ReplyDismissalHandler
from code_review.orchestration.runner_utils import ReviewRunObservability
from code_review.quality.gate import QualityGate
from code_review.schemas.findings import FindingV1
from code_review.schemas.review_decision_event import ReviewDecisionEventContext

logger = logging.getLogger(__name__)


class ReviewDecisionHandler:
    """Decision-only orchestration and review-decision submission flow for one PR."""

    def __init__(
        self,
        pr_ctx: PRContext,
        *,
        dry_run: bool,
        event_context: ReviewDecisionEventContext | None,
        reply_dismissal_handler: ReplyDismissalHandler,
        result_builder: Callable[..., list[FindingV1]],
        skip_if_needed: Callable[..., list[FindingV1] | None],
    ) -> None:
        self.pr_ctx = pr_ctx
        self.dry_run = dry_run
        self.event_context = event_context
        self.reply_dismissal_handler = reply_dismissal_handler
        self._result_builder = result_builder
        self._skip_if_needed = skip_if_needed

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

    def try_skip_when_bot_not_blocking(
        self,
        provider,
        app_cfg,
        run_observability: ReviewRunObservability,
    ) -> list[FindingV1] | None:
        if not (
            app_cfg.review_decision_only_skip_if_bot_not_blocking
            and runner_mod.event_allows_decision_only_skip_when_bot_not_blocking(
                self.event_context
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
        return self._result_builder(
            run_observability,
            paths=[],
            all_findings=[],
            successful_post_count=0,
            to_post=[],
        )

    def try_skip_when_event_actor_is_bot(
        self,
        provider,
        run_observability: ReviewRunObservability,
    ) -> list[FindingV1] | None:
        """Skip bot-authored comment events as a provider-neutral fallback.

        GitHub App webhook parsing should drop obvious bot-authored comment events before
        scheduling work, but non-GitHub integrations and direct runner entry points still
        rely on this late guard.
        """
        ctx = self.event_context
        if ctx is None:
            return None
        actor_login = (ctx.actor_login or "").strip()
        actor_id = (ctx.actor_id or "").strip()
        if not actor_login and not actor_id:
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
        return self._result_builder(
            run_observability,
            paths=[],
            all_findings=[],
            successful_post_count=0,
            to_post=[],
        )

    def resolve_empty_scope_submission_head_sha(
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

    def maybe_finish_empty_scope_review(
        self,
        provider,
        cfg,
        head_sha: str,
        run_observability: ReviewRunObservability,
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
            gate_outcome = QualityGate(provider, self.owner, self.repo, self.pr_number, cfg).evaluate(
                []
            )
            if gate_outcome is not None:
                runner_mod._log_quality_gate_review_outcome("Empty-scope refresh", gate_outcome)
            else:
                logger.warning(
                    "Empty-scope refresh: skipping PR review decision because unresolved "
                    "review-item lookup failed."
                )
            submission_head_sha = self.resolve_empty_scope_submission_head_sha(
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
        return self._result_builder(run_observability, paths, [], 0, [])

    def run_review_decision_only(
        self,
        run_observability: ReviewRunObservability,
        cfg,
        provider,
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
        runner_mod._log_review_decision_event_if_present(self.event_context)
        app_cfg = runner_mod.get_code_review_app_config()

        skip_result = self._skip_if_needed(provider, cfg, run_observability)
        if skip_result is not None:
            return skip_result
        skip_bot_event = self.try_skip_when_event_actor_is_bot(provider, run_observability)
        if skip_bot_event is not None:
            return skip_bot_event
        skip_early = self.try_skip_when_bot_not_blocking(provider, app_cfg, run_observability)
        if skip_early is not None:
            return skip_early
        head_hint = runner_mod._head_sha_hint_for_decision_only(self.head_sha)
        head_sha = runner_mod._resolve_head_sha_for_review_decision_submission(
            provider, self.owner, self.repo, self.pr_number, head_hint
        )
        if not head_sha and not self.dry_run:
            logger.warning(
                "Review-decision-only: head_sha missing after provider lookup; "
                "submit_review_decision may omit commit id for some SCMs."
            )
        excluded_gate = self.reply_dismissal_handler.excluded_gate_ids(
            provider, app_cfg, run_observability.trace_id
        )
        gate_outcome = QualityGate(provider, self.owner, self.repo, self.pr_number, cfg).evaluate(
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
        return self._result_builder(
            run_observability,
            paths=[],
            all_findings=[],
            successful_post_count=0,
            to_post=[],
        )
