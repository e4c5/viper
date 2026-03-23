"""Shared helpers for SCM review decision submission (approve / request changes)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_AUTOMATED_REVIEW_BODY = "Automated review decision by Viper."


def effective_review_body(body: str, *, default: str | None = None) -> str:
    """Return stripped ``body`` or the default automated-review sentence."""
    t = (body or "").strip()
    if t:
        return t
    d = default if default is not None else DEFAULT_AUTOMATED_REVIEW_BODY
    return d


def github_style_pull_review_json(decision: str, body: str, head_sha: str) -> dict[str, Any]:
    """Build JSON body for GitHub- and Gitea-compatible ``POST .../pulls/:id/reviews``."""
    payload: dict[str, Any] = {
        "event": decision,
        "body": effective_review_body(body),
    }
    if head_sha:
        payload["commit_id"] = head_sha
    return payload


def gitlab_note_with_submit_review_requested_changes(body: str) -> str:
    """MR note text that runs GitLab quick action ``/submit_review requested_changes``."""
    text = effective_review_body(body)
    return f"{text}\n\n/submit_review requested_changes"


def delete_soft_fail(
    delete_fn: Callable[[str], None],
    url: str,
    *,
    safe_codes: frozenset[int] = frozenset({404}),
    log_label: str = "",
) -> None:
    """Call ``delete_fn(url)`` and silently swallow HTTP errors whose status is in *safe_codes*.

    Used by providers to clear a stale review state before writing a new decision so that a
    re-run on an updated PR cannot leave the bot in contradictory states (e.g. both approved
    and requesting changes).  A 404 response typically means the bot was not in that state,
    so the default ``safe_codes`` already covers the most common case.
    """
    label = log_label or url
    try:
        delete_fn(url)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else None
        if code in safe_codes:
            return
        logger.warning("DELETE %s failed (HTTP %s): %s", label, code, exc)
    except Exception as exc:
        logger.warning("DELETE %s failed: %s", label, exc)
