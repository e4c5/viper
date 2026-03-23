"""Shared parsing for GitHub- / Gitea-style PR review lists (Phase D bot blocking)."""

from __future__ import annotations

import logging
from typing import Any

from code_review.providers.base import BotBlockingState

logger = logging.getLogger(__name__)


def _norm_review_state(raw: str) -> str:
    return raw.strip().upper().replace(" ", "_").replace("-", "_")


def _mine_github_style_reviews(
    reviews: list[Any], token_login_lower: str
) -> list[tuple[int, str]]:
    mine: list[tuple[int, str]] = []
    for r in reviews:
        if not isinstance(r, dict):
            continue
        user = r.get("user")
        login = ""
        if isinstance(user, dict):
            login = str(user.get("login") or "").strip().lower()
        if not login or login != token_login_lower:
            continue
        rid = int(r.get("id") or 0)
        raw_state = str(r.get("state") or "")
        mine.append((rid, raw_state))
    return mine


def _last_non_pending_review_raw(mine: list[tuple[int, str]]) -> str | None:
    if not mine:
        return None
    mine.sort(key=lambda x: x[0])
    for _rid, raw_state in reversed(mine):
        if _norm_review_state(raw_state) == "PENDING":
            continue
        return raw_state
    return None


def _blocking_from_github_norm(norm: str, last_raw: str) -> BotBlockingState:
    if norm in (
        "CHANGES_REQUESTED",
        "REQUEST_CHANGES",
        "REQUESTED_CHANGES",
    ):
        return "BLOCKING"
    if norm == "APPROVED":
        return "NOT_BLOCKING"
    if norm in ("COMMENT", "COMMENTED", "DISMISSED", ""):
        return "NOT_BLOCKING"
    logger.debug("Unknown GitHub-style PR review state for token user: %r", last_raw)
    return "UNKNOWN"


def blocking_state_from_github_style_reviews(
    reviews: list[Any],
    *,
    token_login_lower: str,
) -> BotBlockingState:
    """Use the latest review authored by *token_login_lower* on this PR/MR.

    GitHub uses ``CHANGES_REQUESTED``; Gitea may use ``REQUEST_CHANGES`` or similar.
    """
    mine = _mine_github_style_reviews(reviews, token_login_lower)
    if not mine:
        return "NOT_BLOCKING"
    last_raw = _last_non_pending_review_raw(mine)
    if last_raw is None:
        return "NOT_BLOCKING"
    return _blocking_from_github_norm(_norm_review_state(last_raw), last_raw)


def blocking_state_from_token_and_github_style_review_list(
    token_login_lower: str,
    reviews: list[Any] | None,
) -> BotBlockingState:
    """Map token login + a fetched GitHub-style review list to blocking state.

    Returns ``UNKNOWN`` when the token user login is unknown or *reviews* is
    ``None`` (listing failed). Otherwise delegates to
    :func:`blocking_state_from_github_style_reviews`.
    """
    if not token_login_lower:
        return "UNKNOWN"
    if reviews is None:
        return "UNKNOWN"
    return blocking_state_from_github_style_reviews(
        reviews, token_login_lower=token_login_lower
    )
