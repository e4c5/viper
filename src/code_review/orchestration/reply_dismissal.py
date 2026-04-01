from __future__ import annotations

import logging
from collections.abc import Callable

from code_review import observability
from code_review import orchestration_deps as runner_mod
from code_review.models import PRContext
from code_review.orchestration.events import (
    ReplyDismissalContext,
    _reply_added_event_authored_by_bot,
    _reply_dismissal_diff_context_for_thread,
    _reply_dismissal_scm_already_addressed_reason,
)
from code_review.providers.base import BotAttributionIdentity
from code_review.schemas.reply_dismissal import ReplyDismissalVerdictV1
from code_review.schemas.review_decision_event import ReviewDecisionEventContext
from code_review.schemas.review_thread_dismissal import ReviewThreadDismissalContext

logger = logging.getLogger(__name__)


class ReplyDismissalHandler:
    """Owns reply-dismissal orchestration and side effects for a single PR."""

    def __init__(
        self,
        pr_ctx: PRContext,
        *,
        dry_run: bool,
        event_context: ReviewDecisionEventContext | None,
        run_reply_dismissal_llm: Callable[[str], str],
    ) -> None:
        self.pr_ctx = pr_ctx
        self.dry_run = dry_run
        self.event_context = event_context
        self._run_reply_dismissal_llm = run_reply_dismissal_llm

    @property
    def owner(self) -> str:
        return self.pr_ctx.owner

    @property
    def repo(self) -> str:
        return self.pr_ctx.repo

    @property
    def pr_number(self) -> int:
        return self.pr_ctx.pr_number

    def maybe_post_disagreed_thread_reply(
        self,
        provider,
        caps_rd,
        comment_id: str,
        verdict: ReplyDismissalVerdictV1,
    ) -> None:
        if not caps_rd.supports_review_thread_reply:
            logger.info("Reply-dismissal disagreed: provider does not support thread replies")
            return
        if self.dry_run:
            truncated = (verdict.reply_text or "")[:500]
            logger.info("Dry-run: would post review-thread reply (truncated): %s", truncated)
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

    def maybe_post_agreed_thread_reply(
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

    def maybe_resolve_agreed_thread(
        self,
        provider,
        caps_rd,
        comment_id: str,
        dctx: ReviewThreadDismissalContext,
    ) -> bool:
        if not caps_rd.supports_review_thread_resolution:
            return self.maybe_post_agreed_thread_reply(provider, caps_rd, comment_id)
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
            return self.maybe_post_agreed_thread_reply(provider, caps_rd, comment_id)

    def comment_id_or_none(self, app_cfg) -> str | None:
        """Return event comment id when reply-dismissal should run, else ``None``."""
        ctx = self.event_context
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

    def precheck(
        self, provider, comment_id: str
    ) -> tuple[BotAttributionIdentity, ReviewThreadDismissalContext] | None:
        """Return bot identity and dismissal context when reply-dismissal can proceed."""
        ctx = self.event_context
        if ctx is None:
            logger.info("Reply-dismissal skipped: no event context present")
            return None
        logger.info("Reply-dismissal candidate: loading thread context for comment_id=%s", comment_id)
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
    def parse_verdict(raw_verdict: str) -> ReplyDismissalVerdictV1 | None:
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

    def diff_context(self, provider, dctx: ReviewThreadDismissalContext) -> str:
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

    def run_llm_and_parse(self, user_msg: str) -> ReplyDismissalVerdictV1 | None:
        try:
            raw_verdict = self._run_reply_dismissal_llm(user_msg)
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
        return self.parse_verdict(raw_verdict)

    def excluded_gate_ids_from_verdict(
        self,
        provider,
        comment_id: str,
        dctx: ReviewThreadDismissalContext,
        verdict: ReplyDismissalVerdictV1,
    ) -> frozenset[str]:
        if verdict.verdict == "agreed":
            observability.record_reply_dismissal_outcome("agreed")
            persisted = self.maybe_resolve_agreed_thread(
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
            self.maybe_post_disagreed_thread_reply(
                provider, provider.capabilities(), comment_id, verdict
            )
        return frozenset()

    def excluded_gate_ids(
        self,
        provider,
        app_cfg,
        trace_id: str,
    ) -> frozenset[str]:
        """Stable ids to exclude from the quality gate after optional reply-dismissal LLM."""
        comment_id = self.comment_id_or_none(app_cfg)
        if comment_id is None:
            return frozenset()
        precheck = self.precheck(provider, comment_id)
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
        diff_context = self.diff_context(provider, dctx)
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
        verdict = self.run_llm_and_parse(user_msg)
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
        return self.excluded_gate_ids_from_verdict(provider, comment_id, dctx, verdict)
