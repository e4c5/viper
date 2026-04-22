from __future__ import annotations

import logging
from typing import Any

from code_review import orchestration_deps as runner_mod
from code_review.models import PRContext

logger = logging.getLogger(__name__)


class ContextEnricher:
    """Context-aware prompt enrichment for a single PR review run."""

    def __init__(self, pr_ctx: PRContext) -> None:
        self.pr_ctx = pr_ctx

    @property
    def owner(self) -> str:
        return self.pr_ctx.owner

    @property
    def repo(self) -> str:
        return self.pr_ctx.repo

    @property
    def pr_number(self) -> int:
        return self.pr_ctx.pr_number

    def validate_context_sources_or_raise(self, ctx_cfg, cfg) -> None:
        """Validate external context-source configuration when context-aware review is enabled."""
        if not ctx_cfg.enabled:
            return
        runner_mod.validate_context_aware_sources(ctx_cfg, cfg)

    def load_commit_messages(self, provider, need_commits: bool) -> list[str]:
        raw = (
            provider.get_pr_commit_messages(self.owner, self.repo, self.pr_number)
            if need_commits
            else []
        )
        return raw if isinstance(raw, list) else []

    def build_prompt_suffix(
        self,
        provider,
        cfg,
        ctx_cfg,
        app_cfg,
        pr_info_for_metadata: Any,
        full_diff: str,
        remaining_tokens: int,
    ) -> tuple[list[object], str | None, str]:
        need_commits = app_cfg.include_commit_messages_in_prompt or ctx_cfg.enabled
        commit_messages = self.load_commit_messages(provider, need_commits)
        pr_title = (pr_info_for_metadata.title if pr_info_for_metadata else "") or ""
        pr_desc = (pr_info_for_metadata.description if pr_info_for_metadata else "") or ""
        refs = (
            runner_mod.extract_context_references(
                cfg.provider,
                self.owner,
                self.repo,
                [pr_title, pr_desc, *commit_messages],
                extract_github=ctx_cfg.github_issues_enabled,
                extract_gitlab=ctx_cfg.gitlab_issues_enabled,
                extract_jira=ctx_cfg.jira_enabled,
                extract_confluence=ctx_cfg.confluence_enabled,
            )
            if ctx_cfg.enabled
            else []
        )
        context_brief: str | None = None
        if ctx_cfg.enabled and refs:
            context_brief = runner_mod.build_context_brief_for_pr(ctx_cfg, cfg, refs, full_diff)
        prompt_suffix = runner_mod._format_review_prompt_supplement(
            context_brief=context_brief,
            context_references=refs,
            commit_messages=commit_messages,
            include_commit_messages=app_cfg.include_commit_messages_in_prompt,
            remaining_tokens=remaining_tokens,
        )
        return (refs, context_brief, prompt_suffix)
