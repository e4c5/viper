"""PR skip-label and title-pattern filter."""
from __future__ import annotations

import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)


def configured_skip_labels(cfg) -> list[str]:
    """Skip labels from config (SCMConfig.skip_labels when available, else parse)."""
    skip_labels_fn = getattr(cfg, "skip_labels", None)
    if callable(skip_labels_fn):
        labels = skip_labels_fn()
        if isinstance(labels, list):
            return [lb for lb in labels if isinstance(lb, str) and lb.strip()]
    raw = getattr(cfg, "skip_label", "")
    if not isinstance(raw, str):
        return []
    return [lb.strip() for lb in raw.split(",") if lb.strip()]


class ReviewFilter:
    """Decides whether a PR should be skipped before the review begins."""

    def should_skip(self, pr_info, cfg) -> str | None:
        """Return a skip reason string (or None) based on skip labels and title patterns.

        Returns:
            None if the PR should proceed with review
            A non-empty string explaining why the PR should be skipped
        """
        skip_labels = configured_skip_labels(cfg)
        if not skip_labels and not cfg.skip_title_pattern:
            return None
        if not pr_info:
            return None

        raw_labels = getattr(pr_info, "labels", [])
        labels = (
            [
                label
                for label in raw_labels
                if isinstance(label, str) and label.strip()
            ]
            if isinstance(raw_labels, Iterable) and not isinstance(raw_labels, str | bytes)
            else []
        )

        if skip_labels and labels:
            lowered = {lb.strip().lower() for lb in labels}
            matched = next(
                (lb for lb in skip_labels if lb.lower() in lowered), None
            )
            if matched is not None:
                return f"PR has skip label: {matched}"

        skip_title_pattern = (
            cfg.skip_title_pattern.strip() if isinstance(cfg.skip_title_pattern, str) else ""
        )
        title = getattr(pr_info, "title", "")
        normalized_title = title.strip().lower() if isinstance(title, str) and title.strip() else ""

        if (
            skip_title_pattern
            and normalized_title
            and skip_title_pattern.lower() in normalized_title
        ):
            return f"PR title matches skip pattern: {cfg.skip_title_pattern}"

        return None
