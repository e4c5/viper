"""Bot identity checks and reply-dismissal helpers.

Module-level functions: `_event_actor_matches_*`, `_reply_added_event_authored_by_bot`,
small `_reply_dismissal_entry_*` helpers, `_reply_dismissal_diff_context_for_thread`,
`_reply_dismissal_scm_already_addressed_reason`.

`ReplyDismissalContext` (frozen dataclass): pure derivations from the immutable pair
(ReviewThreadDismissalContext, BotAttributionIdentity) — same value-object pattern as
PRContext. No I/O ever goes on this class.
"""

from __future__ import annotations

from dataclasses import dataclass

from code_review.diff.parser import annotate_diff_with_line_numbers
from code_review.diff.position import get_diff_hunk_for_line
from code_review.formatters.comment import infer_severity_from_comment_body
from code_review.providers.base import BotAttributionIdentity, unified_diff_for_path
from code_review.schemas.review_decision_event import ReviewDecisionEventContext
from code_review.schemas.review_thread_dismissal import ReviewThreadDismissalContext

# ---------------------------------------------------------------------------
# Module-level helpers (no shared state)
# ---------------------------------------------------------------------------

def _normalize_scm_identity_fragment(value: str) -> str:
    """Lowercase and strip braces/spaces for comparing SCM user/uuid strings."""
    return (value or "").strip().lower().replace("{", "").replace("}", "")


def _event_actor_matches_bot_login(actor_login: str, bot: BotAttributionIdentity) -> bool:
    bot_login = (bot.login or "").strip()
    return bool(bot_login and actor_login and actor_login.lower() == bot_login.lower())


def _event_actor_matches_bot_id(actor_id: str, bot: BotAttributionIdentity) -> bool:
    bot_id_str = (bot.id_str or "").strip()
    return bool(bot_id_str and actor_id and actor_id == bot_id_str)


def _event_actor_matches_bot_slug(actor_login: str, bot: BotAttributionIdentity) -> bool:
    bot_slug = (bot.slug or "").strip()
    return bool(bot_slug and actor_login and actor_login.lower() == bot_slug.lower())


def _event_actor_matches_bot_uuid_fragments(
    actor_id: str, actor_login: str, bot: BotAttributionIdentity
) -> bool:
    bot_uuid = _normalize_scm_identity_fragment(bot.uuid)
    if not bot_uuid:
        return False
    actor_uuid = _normalize_scm_identity_fragment(actor_id)
    if actor_uuid and bot_uuid == actor_uuid:
        return True
    actor_login_uuid = _normalize_scm_identity_fragment(actor_login)
    return bool(actor_login_uuid and bot_uuid == actor_login_uuid)


def _reply_added_event_authored_by_bot(
    event: ReviewDecisionEventContext, bot: BotAttributionIdentity
) -> bool:
    """True when event actor fields identify the same user as the review token (bot)."""
    if not bot.is_resolved():
        return False
    actor_login = (event.actor_login or "").strip()
    actor_id = (event.actor_id or "").strip()
    if not actor_login and not actor_id:
        return False
    if _event_actor_matches_bot_login(actor_login, bot):
        return True
    if _event_actor_matches_bot_id(actor_id, bot):
        return True
    if _event_actor_matches_bot_slug(actor_login, bot):
        return True
    return _event_actor_matches_bot_uuid_fragments(actor_id, actor_login, bot)


def _reply_dismissal_entry_is_bot_authored(
    author_login: str,
    bot: BotAttributionIdentity,
) -> bool:
    """Best-effort match for thread entries, which usually expose only a login/slug-like field."""
    actor_login = (author_login or "").strip()
    if not actor_login or not bot.is_resolved():
        return False
    if _event_actor_matches_bot_login(actor_login, bot):
        return True
    if _event_actor_matches_bot_slug(actor_login, bot):
        return True
    return _event_actor_matches_bot_uuid_fragments("", actor_login, bot)


def _reply_dismissal_scm_already_addressed_reason(
    ctx: ReviewThreadDismissalContext,
) -> str:
    """Provider-supplied reason when SCM already indicates the concern is addressed."""
    if not bool(getattr(ctx, "scm_already_addressed", False)):
        return ""
    return (getattr(ctx, "scm_already_addressed_reason", "") or "").strip() or "scm_state"


def _reply_dismissal_diff_context_for_thread(
    full_diff: str,
    ctx: ReviewThreadDismissalContext,
) -> str:
    """Return an annotated diff snippet for the thread's anchored file/line when available."""
    path = (ctx.path or "").strip()
    if not full_diff or not path:
        return ""
    line = int(ctx.line or 0)
    diff_text = get_diff_hunk_for_line(full_diff, path, line) if line > 0 else None
    if not diff_text:
        diff_text = unified_diff_for_path(full_diff, path)
    diff_text = (diff_text or "").strip()
    if not diff_text:
        return ""
    annotated = annotate_diff_with_line_numbers(diff_text)
    if len(annotated) > 12_000:
        annotated = annotated[:11_999] + "…"
    lines = [
        "",
        "Relevant PR diff context:",
        f"Anchored file: {path}",
    ]
    if line > 0:
        lines.append(f"Anchored line: {line}")
    lines.extend(["", "```diff", annotated, "```"])
    return "\n".join(lines)


def _reply_dismissal_entry_tags(
    ent,
    *,
    original_comment_id: str,
    triggered_comment_id: str,
    bot: BotAttributionIdentity,
) -> list[str]:
    tags: list[str] = []
    cid = (ent.comment_id or "").strip()
    if cid and cid == original_comment_id:
        tags.append("original automated review comment")
    if cid and cid == triggered_comment_id:
        tags.append("triggering human reply")
    if _reply_dismissal_entry_is_bot_authored(ent.author_login, bot):
        tags.append("bot-authored")
    return tags


def _reply_dismissal_entry_lines(
    ent,
    index: int,
    *,
    original_comment_id: str,
    triggered_comment_id: str,
    bot: BotAttributionIdentity,
) -> list[str]:
    lines = [f"--- Comment {index} ---"]
    tags = _reply_dismissal_entry_tags(
        ent,
        original_comment_id=original_comment_id,
        triggered_comment_id=triggered_comment_id,
        bot=bot,
    )
    cid = (ent.comment_id or "").strip()
    if tags:
        lines.append(f"Role: {', '.join(tags)}")
    if cid:
        lines.append(f"Comment id: {cid}")
    lines.append(f"Author: {(ent.author_login or '').strip() or '(unknown)'}")
    lines.append(ent.body or "")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# ReplyDismissalContext — value object: pure derivations, no I/O
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReplyDismissalContext:
    """Pure derivations from the immutable pair (thread context, bot identity).

    Same value-object principle as PRContext: no I/O ever goes on this class.
    """

    ctx: ReviewThreadDismissalContext
    bot: BotAttributionIdentity

    @property
    def original_comment_id(self) -> str:
        """Prefer the first bot-authored entry; fall back to the first thread entry."""
        for ent in self.ctx.entries:
            if _reply_dismissal_entry_is_bot_authored(ent.author_login, self.bot):
                return (ent.comment_id or "").strip()
        if self.ctx.entries:
            return (self.ctx.entries[0].comment_id or "").strip()
        return ""

    @property
    def original_comment_severity(self) -> str:
        """Infer severity from the original automated review comment body when possible."""
        orig_id = self.original_comment_id
        for ent in self.ctx.entries:
            if (ent.comment_id or "").strip() == orig_id:
                return infer_severity_from_comment_body(ent.body or "")
        if self.ctx.entries:
            return infer_severity_from_comment_body(self.ctx.entries[0].body or "")
        return "unknown"

    def has_bot_authored_entry(self) -> bool:
        """Return True if at least one thread entry is authored by the bot.

        When the bot identity is unresolved we cannot verify authorship, so
        we return True (safe fallback: continue processing the thread rather
        than silently skipping it).
        """
        if not self.bot.is_resolved():
            return True
        return any(
            _reply_dismissal_entry_is_bot_authored(ent.author_login, self.bot)
            for ent in self.ctx.entries
        )

    def existing_bot_reply_after_trigger(self, triggering_comment_id: str):
        """Return a later bot-authored thread entry when this trigger was already handled."""
        triggered_comment_id = (triggering_comment_id or "").strip()
        if not triggered_comment_id:
            return None
        seen_trigger = False
        for ent in self.ctx.entries:
            cid = (ent.comment_id or "").strip()
            if cid and cid == triggered_comment_id:
                seen_trigger = True
                continue
            if seen_trigger and _reply_dismissal_entry_is_bot_authored(ent.author_login, self.bot):
                return ent
        return None

    def format_user_message(
        self,
        triggering_comment_id: str,
        diff_context: str = "",
    ) -> str:
        """Build the user message for the reply-dismissal agent."""
        who = (
            self.bot.login or self.bot.slug or self.bot.id_str or self.bot.uuid or ""
        ).strip() or "(unknown)"
        orig_id = self.original_comment_id
        orig_severity = self.original_comment_severity
        triggered_comment_id = (triggering_comment_id or "").strip()
        lines = [
            "Classify this single pull-request review thread.",
            f"Automated reviewer identity hint (token user): {who}",
            f"Original automated review comment id: {orig_id or '(unknown)'}",
            f"Original automated review comment severity: {orig_severity}",
            f"Triggering human reply comment id: {triggered_comment_id or '(unknown)'}",
            "",
            "Thread comments in chronological order:",
        ]
        for i, ent in enumerate(self.ctx.entries, start=1):
            lines.extend(
                _reply_dismissal_entry_lines(
                    ent,
                    i,
                    original_comment_id=orig_id,
                    triggered_comment_id=triggered_comment_id,
                    bot=self.bot,
                )
            )
        if diff_context:
            lines.append(diff_context)
        return "\n".join(lines)
