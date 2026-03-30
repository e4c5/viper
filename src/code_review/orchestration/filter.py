"""PR skip-label and title-pattern filter."""
from __future__ import annotations

import logging

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

        if (
            cfg.skip_label
            and cfg.skip_label.strip()
            and any(
                lb.strip().lower() == cfg.skip_label.strip().lower() for lb in pr_info.labels
            )
        ):
            return f"PR has skip label: {cfg.skip_label}"

        if (
            cfg.skip_title_pattern
            and cfg.skip_title_pattern.strip()
            and cfg.skip_title_pattern.strip().lower() in pr_info.title.lower()
        ):
            return f"PR title matches skip pattern: {cfg.skip_title_pattern}"

        return None
