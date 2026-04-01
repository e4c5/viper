"""PR skip-label and title-pattern filter."""
from __future__ import annotations

import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)


class ReviewFilter:
    """Decides whether a PR should be skipped before the review begins."""

    def should_skip(self, pr_info, cfg) -> str | None:
        """Return a skip reason string (or None) based on skip labels and title patterns.

        Returns:
            None if the PR should proceed with review
            A non-empty string explaining why the PR should be skipped
        """
        if not cfg.skip_label and not cfg.skip_title_pattern:
            return None
        if not pr_info:
            return None

        skip_label = cfg.skip_label.strip() if isinstance(cfg.skip_label, str) else ""
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

        if (
            skip_label
            and labels
            and any(lb.strip().lower() == skip_label.lower() for lb in labels)
        ):
            return f"PR has skip label: {cfg.skip_label}"

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
