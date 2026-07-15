"""Shared request-scoped runtime helpers for webhook edge services."""

from __future__ import annotations

from pydantic import SecretStr

from code_review.config import get_code_review_app_config, get_scm_config
from code_review.runner import ReviewDecisionConfig, run_review
from code_review.service_models import ServiceReviewJob


class RequestScopedReviewRunner:
    """Thin wrapper that invokes ``run_review`` with request-scoped config objects."""

    def __init__(self, *, scm_provider: str, scm_url: str, dry_run: bool = False) -> None:
        self.scm_provider = str(scm_provider or "").strip()
        self.scm_url = str(scm_url or "").strip().rstrip("/")
        self.dry_run = dry_run

    def _scm_config_for_job(
        self,
        job: ServiceReviewJob,
        scm_token: str,
        *,
        bot_login: str = "",
    ):
        base_scm = get_scm_config()
        return base_scm.model_copy(
            update={
                "provider": self.scm_provider,
                "url": self.scm_url,
                "token": SecretStr(scm_token),
                "base_sha": job.base_sha,
                "bot_identity": (bot_login or "").strip(),
            }
        )

    def run_job(
        self,
        job: ServiceReviewJob,
        scm_token: str,
        bot_login: str = "",
        *,
        started_review_notice_posted: bool = False,
    ) -> None:
        """Execute one review job with request-scoped SCM credentials."""
        scm_cfg = self._scm_config_for_job(
            job,
            scm_token,
            bot_login=bot_login,
        )
        app_cfg = get_code_review_app_config().model_copy(
            update={"started_review_comment_posted": bool(started_review_notice_posted)}
        )
        run_review(
            owner=job.owner,
            repo=job.repo,
            pr_number=job.pr_number,
            head_sha=job.head_sha,
            dry_run=self.dry_run,
            review_decision=ReviewDecisionConfig(
                only=job.review_decision_only,
                event_context=job.event_context,
            ),
            scm_config=scm_cfg,
            app_config=app_cfg,
        )
