from __future__ import annotations

from code_review import orchestration_deps as runner_mod


class ReviewOrchestrator:
    """Orchestrates a single code review run (findings-only mode)."""

    def __init__(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str = "",
        *,
        dry_run: bool = False,
        print_findings: bool = False,
        review_decision_enabled: bool | None = None,
        review_decision_high_threshold: int | None = None,
        review_decision_medium_threshold: int | None = None,
        review_decision_only: bool = False,
        event_context: runner_mod.ReviewDecisionEventContext | None = None,
    ):
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.head_sha = head_sha
        self.dry_run = dry_run
        self.print_findings = print_findings
        self._review_decision_enabled_override = review_decision_enabled
        self._review_decision_high_threshold_override = review_decision_high_threshold
        self._review_decision_medium_threshold_override = review_decision_medium_threshold
        self._review_decision_only = review_decision_only
        self._event_context = event_context

    def _load_config_and_provider(self):
        """Load SCM/LLM config and create the provider instance.

        Returns (cfg, llm_cfg, provider).
        """
        cfg = runner_mod.get_scm_config()
        overrides: dict[str, bool | int] = {}
        if self._review_decision_enabled_override is not None:
            overrides["review_decision_enabled"] = self._review_decision_enabled_override
        if self._review_decision_high_threshold_override is not None:
            overrides["review_decision_high_threshold"] = (
                self._review_decision_high_threshold_override
            )
        if self._review_decision_medium_threshold_override is not None:
            overrides["review_decision_medium_threshold"] = (
                self._review_decision_medium_threshold_override
            )
        if overrides:
            cfg = cfg.model_copy(update=overrides)
        llm_cfg = runner_mod.get_llm_config()
        token_val = (
            cfg.token.get_secret_value() if hasattr(cfg.token, "get_secret_value") else cfg.token
        )
        provider = runner_mod.get_provider(
            cfg.provider,
            cfg.url,
            token_val,
            bitbucket_server_user_slug=cfg.bitbucket_server_user_slug,
        )
        return (cfg, llm_cfg, provider)

    def _determine_skip_reason(
        self,
        provider,
        cfg,
        trace_id: str,
        start_time: float,
        run_handle,
    ) -> list[runner_mod.FindingV1] | None:
        """
        If the PR should be skipped (skip label or title pattern), emit observability and return [].
        Otherwise return None (caller continues).
        """
        if not cfg.skip_label and (not cfg.skip_title_pattern):
            return None
        pr_info = provider.get_pr_info(self.owner, self.repo, self.pr_number)
        if not pr_info:
            return None
        if (
            cfg.skip_label
            and cfg.skip_label.strip()
            and any(
                lb.strip().lower() == cfg.skip_label.strip().lower() for lb in pr_info.labels
            )
        ):
            _duration_ms = (runner_mod.time.perf_counter() - start_time) * 1000
            runner_mod._log_run_complete(
                trace_id, self.owner, self.repo, self.pr_number, 0, 0, 0, _duration_ms
            )
            runner_mod.observability.finish_run(
                run_handle,
                self.owner,
                self.repo,
                self.pr_number,
                0,
                0,
                0,
                _duration_ms / 1000.0,
            )
            return []
        if (
            cfg.skip_title_pattern
            and cfg.skip_title_pattern.strip()
            and (cfg.skip_title_pattern.strip().lower() in pr_info.title.lower())
        ):
            _duration_ms = (runner_mod.time.perf_counter() - start_time) * 1000
            runner_mod._log_run_complete(
                trace_id, self.owner, self.repo, self.pr_number, 0, 0, 0, _duration_ms
            )
            runner_mod.observability.finish_run(
                run_handle,
                self.owner,
                self.repo,
                self.pr_number,
                0,
                0,
                0,
                _duration_ms / 1000.0,
            )
            return []
        return None

    def _load_existing_comments_and_markers(self, provider):
        """
        Fetch existing review comments, build ignore set and resolved sets from markers.
        Returns (existing, existing_dicts, ignore_set, resolved_comments,
                 resolved_body_set, resolved_fp_set).
        """
        existing = provider.get_existing_review_comments(self.owner, self.repo, self.pr_number)
        existing_dicts = [c.model_dump() for c in existing]
        ignore_set = runner_mod._build_ignore_set(existing_dicts)
        resolved_comments = []
        for c in existing:
            resolved_flag = getattr(c, "resolved", False)
            if isinstance(resolved_flag, bool) and resolved_flag:
                resolved_comments.append(c)
        resolved_body_set: set[tuple[str, str]] = set()
        resolved_fp_set: set[tuple[str, str]] = set()
        for c in resolved_comments:
            path = getattr(c, "path", "") or ""
            body = getattr(c, "body", "") or ""
            if not path or not body:
                continue
            body_hash = runner_mod.hashlib.sha256(body.encode()).hexdigest()
            resolved_body_set.add((path, body_hash))
            parsed = runner_mod.parse_marker_from_comment_body(body)
            if parsed.get("fingerprint"):
                resolved_fp_set.add((path, parsed["fingerprint"]))
        return (
            existing,
            existing_dicts,
            ignore_set,
            resolved_comments,
            resolved_body_set,
            resolved_fp_set,
        )

    def _compute_idempotency_and_maybe_short_circuit(
        self,
        cfg,
        llm_cfg,
        existing_dicts: list,
        trace_id: str,
        start_time: float,
        run_handle,
        incremental_base_sha: str = "",
    ) -> list[runner_mod.FindingV1] | None:
        """
        If we already ran for this PR/range/config (run id in comment marker),
        emit observability and return []. Otherwise return None (caller continues).
        """
        if not self.head_sha:
            return None
        incremental_base_sha = incremental_base_sha or self._incremental_base_sha(
            cfg, self.head_sha
        )
        run_id = runner_mod._build_idempotency_key(
            cfg,
            llm_cfg,
            self.owner,
            self.repo,
            self.pr_number,
            self.head_sha,
            incremental_base_sha,
        )
        if not runner_mod._idempotency_key_seen_in_comments(existing_dicts, run_id):
            return None
        _duration_ms = (runner_mod.time.perf_counter() - start_time) * 1000
        runner_mod._log_run_complete(
            trace_id, self.owner, self.repo, self.pr_number, 0, 0, 0, _duration_ms
        )
        runner_mod.observability.finish_run(
            run_handle,
            self.owner,
            self.repo,
            self.pr_number,
            0,
            0,
            0,
            _duration_ms / 1000.0,
        )
        return []

    @staticmethod
    def _incremental_base_sha(cfg, head_sha: str) -> str:
        """Return a usable incremental review base SHA, else ``""`` for full-PR review."""
        raw_base_sha = getattr(cfg, "base_sha", "")
        base_sha = raw_base_sha.strip() if isinstance(raw_base_sha, str) else ""
        raw_head_sha = head_sha if head_sha else getattr(cfg, "head_sha", "")
        head_sha = raw_head_sha.strip() if isinstance(raw_head_sha, str) else ""
        if not base_sha or not head_sha or base_sha == head_sha:
            return ""
        return base_sha

    def _fetch_pr_files_and_diffs(self, provider):
        """Fetch the full-PR file list and diff.

        Retained as a small compatibility wrapper for tests and helper callers;
        incremental review flow uses ``_fetch_review_files_and_diffs``.
        """
        files = provider.get_pr_files(self.owner, self.repo, self.pr_number)
        paths = [f.path for f in files]
        full_diff = provider.get_pr_diff(self.owner, self.repo, self.pr_number)
        return (files, paths, full_diff)

    def _fetch_review_files_and_diffs(
        self, provider, cfg
    ) -> tuple[list[object], list[str], str, str]:
        """Fetch the file list and diff for the active review scope.

        Returns ``(files, paths, full_diff, incremental_base_sha)`` where
        ``incremental_base_sha`` is non-empty only when the review was scoped to
        ``SCM_BASE_SHA..SCM_HEAD_SHA``.
        """
        base_sha = self._incremental_base_sha(cfg, self.head_sha)
        if base_sha:
            files = provider.get_incremental_pr_files(
                self.owner, self.repo, self.pr_number, base_sha, self.head_sha
            )
            paths = [f.path for f in files]
            full_diff = provider.get_incremental_pr_diff(
                self.owner, self.repo, self.pr_number, base_sha, self.head_sha
            )
            return (files, paths, full_diff, base_sha)
        files = provider.get_pr_files(self.owner, self.repo, self.pr_number)
        paths = [f.path for f in files]
        full_diff = provider.get_pr_diff(self.owner, self.repo, self.pr_number)
        return (files, paths, full_diff, "")

    def _build_ignore_set_and_filter_files(self, paths: list[str]) -> list[str]:
        """
        Optionally filter which file paths to review (e.g. by ignore patterns).
        Currently returns paths unchanged; ignore_set is built in
        _load_existing_comments_and_markers and used later to filter findings.
        """
        return paths

    def _detect_languages_for_files(self, paths: list[str]):
        """Run language detection on paths and return (detected, review_standards)."""
        detected = runner_mod.detect_from_paths(paths)
        review_standards = runner_mod.get_review_standards(detected.language, detected.framework)
        return (detected, review_standards)

    def _create_agent_and_runner(
        self,
        provider,
        review_standards: str,
        *,
        use_file_by_file: bool = False,
        disable_tools: bool | None = None,
        context_brief_attached: bool = False,
    ):
        """
        Build the findings-only agent, session service, and ADK Runner.
        Returns (session_id, session_service, runner).

        IMPORTANT: this method must be called AFTER determining the execution mode,
        because the mode directly controls tool and instruction configuration:

        - Single-shot mode (use_file_by_file=False): tools are disabled and the
          SINGLE_SHOT_INSTRUCTION is used.  The full diff is already embedded in
          the user message — no tools are needed.  Enabling tools here causes the
          LLM to make per-file tool calls; each call appends to the ADK session
          history, and every subsequent LLM turn re-bills all prior context
          (triangular token growth → millions of billed tokens on large PRs).
          Additionally, FINDINGS_ONLY_INSTRUCTION references tool names that are
          absent in this mode; Gemini infers it cannot complete the workflow and
          returns [] (no findings).

        - File-by-file mode (use_file_by_file=True): tools are enabled and
          FINDINGS_ONLY_INSTRUCTION is used.  The agent calls get_pr_diff_for_file
          per file, which is the expected workflow.

        - Incremental large-diff mode may still set ``use_file_by_file=True`` but
          also force ``disable_tools=True`` so each per-file prompt embeds the
          already-scoped diff slice rather than fetching the file's full PR diff.
        """
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        disable_tools_effective = (
            disable_tools if disable_tools is not None else not use_file_by_file
        )
        agent = runner_mod.create_review_agent(
            provider,
            review_standards,
            findings_only=True,
            disable_tools=disable_tools_effective,
            context_brief_attached=context_brief_attached,
        )
        session_id = (
            f"{self.owner}/{self.repo}/pr-{self.pr_number}/{runner_mod.uuid.uuid4().hex[:12]}"
        )
        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name=runner_mod.APP_NAME, session_service=session_service)
        return (session_id, session_service, runner)

    def _run_agent_and_collect_findings(
        self,
        runner,
        session_service,
        session_id: str,
        paths: list[str],
        use_file_by_file: bool,
        full_diff: str = "",
        prompt_suffix: str = "",
        diff_by_path: dict[str, str] | None = None,
    ) -> list[runner_mod.FindingV1]:
        """
        Run the agent (file-by-file or single shot), parse response into FindingV1 list.
        Returns all_findings (unfiltered).

        When CODE_REVIEW_LOG_LEVEL=DEBUG, log exactly what we send to the LLM
        (the user message text) and the raw text we receive back, before any
        JSON parsing or filtering.
        """
        if use_file_by_file and paths:
            if diff_by_path is not None:
                return self._run_embedded_file_by_file_mode(
                    runner,
                    session_service,
                    paths,
                    diff_by_path=diff_by_path,
                    prompt_suffix=prompt_suffix,
                )
            return self._run_file_by_file_mode(
                runner,
                session_service,
                paths,
                prompt_suffix=prompt_suffix,
            )
        return self._run_single_shot_mode(
            runner,
            session_service,
            session_id,
            full_diff,
            prompt_suffix=prompt_suffix,
        )

    def _run_embedded_file_by_file_mode(
        self,
        runner,
        session_service,
        paths: list[str],
        *,
        diff_by_path: dict[str, str],
        prompt_suffix: str = "",
    ) -> list[runner_mod.FindingV1]:
        """Review one file at a time using already-scoped embedded diffs (tools disabled)."""

        def _build_embedded_message(file_path: str) -> str | None:
            file_diff = diff_by_path.get(file_path, "")
            if not file_diff:
                return None
            head_sha_clause = f" head_sha={self.head_sha}." if self.head_sha else ""
            msg = (
                "Review exactly one file from this PR. "
                f"owner={self.owner}, repo={self.repo}, pr_number={self.pr_number}."
                + head_sha_clause
                + f' Use path "{file_path}" in every finding.'
                + " The diff below is already scoped to the current review range for this file."
                + " Output a JSON array of findings for this file only."
                + " If there are no issues in this file, output exactly []."
            )
            annotated = runner_mod.annotate_diff_with_line_numbers(file_diff)
            return msg + f"\n\nHere is the unified diff for this file:\n```diff\n{annotated}\n```"

        return self._run_per_file_review_loop(
            runner,
            session_service,
            paths,
            build_message=_build_embedded_message,
            prompt_suffix=prompt_suffix,
            debug_mode_label="embedded file-by-file",
        )

    def _run_file_by_file_mode(
        self,
        runner,
        session_service,
        paths: list[str],
        *,
        prompt_suffix: str = "",
    ) -> list[runner_mod.FindingV1]:
        def _build_file_by_file_message(file_path: str) -> str:
            head_sha_clause = f" head_sha={self.head_sha}." if self.head_sha else ""
            ref_guidance = (
                " When calling get_file_lines for surrounding context, "
                + f'use ref="{self.head_sha}" as the ref parameter.'
                if self.head_sha
                else ""
            )
            annotation_guidance = (
                " The diff returned by get_pr_diff_for_file has <L{n}> line-number "
                "annotations on every visible line (added '+' and context ' '). "
                "Use the <L{n}> value as the 'line' field in each finding. "
                "Do NOT compute line numbers from hunk headers."
            )
            msg = (
                "Review exactly one file from this PR. "
                f"owner={self.owner}, repo={self.repo}, pr_number={self.pr_number}."
                + head_sha_clause
                + " Call get_pr_diff_for_file(owner, repo, pr_number, "
                + f'"{file_path}") to get the diff for this file.'
                + annotation_guidance
                + ref_guidance
                + " Then output a JSON array of findings for this file only. "
                + f'Use path "{file_path}" in every finding. '
                + "If there are no issues in this file, output exactly []."
            )
            return msg

        return self._run_per_file_review_loop(
            runner,
            session_service,
            paths,
            build_message=_build_file_by_file_message,
            prompt_suffix=prompt_suffix,
            debug_mode_label="file-by-file",
        )

    def _run_per_file_review_loop(
        self,
        runner,
        session_service,
        paths: list[str],
        *,
        build_message: runner_mod.Callable[[str], str | None],
        prompt_suffix: str,
        debug_mode_label: str,
    ) -> list[runner_mod.FindingV1]:
        """Run isolated per-file review sessions with shared logging and error handling."""
        all_findings: list[runner_mod.FindingV1] = []
        for file_path in paths:
            file_session_id = (
                f"{self.owner}/{self.repo}/pr-{self.pr_number}/file/{runner_mod.uuid.uuid4().hex[:12]}"
            )
            msg = build_message(file_path)
            if not msg:
                continue
            if prompt_suffix:
                msg = msg + "\n\n" + prompt_suffix
            if runner_mod.logger.isEnabledFor(runner_mod.logging.DEBUG):
                runner_mod.logger.debug(
                    "LLM request (%s) session=%s file=%s prompt=%s",
                    debug_mode_label,
                    file_session_id,
                    file_path,
                    msg,
                )
            content = runner_mod.types.Content(role="user", parts=[runner_mod.types.Part(text=msg)])
            try:
                response_text = runner_mod._run_agent_and_collect_response(
                    runner, session_service, file_session_id, content
                )
            except runner_mod.AuthenticationError as e:
                runner_mod.logger.error(
                    "LLM authentication failed while reviewing file=%s; aborting run: %s",
                    file_path,
                    e,
                )
                raise
            except runner_mod.RateLimitError as e:
                runner_mod.logger.warning(
                    "Rate limit hit while reviewing file=%s (skipping): %s",
                    file_path,
                    e,
                )
                continue
            except Exception as e:
                runner_mod.logger.warning(
                    "Error reviewing file=%s (skipping): %s",
                    file_path,
                    e,
                    exc_info=runner_mod.logger.isEnabledFor(runner_mod.logging.DEBUG),
                )
                continue
            all_findings.extend(runner_mod._findings_from_response(response_text))
        return all_findings

    def _run_single_shot_mode(
        self,
        runner,
        session_service,
        session_id: str,
        full_diff: str,
        *,
        prompt_suffix: str = "",
    ) -> list[runner_mod.FindingV1]:
        msg = (
            "Review this PR: "
            f"owner={self.owner}, repo={self.repo}, pr_number={self.pr_number}."
            + (f" head_sha={self.head_sha}." if self.head_sha else "")
        )
        if full_diff:
            annotated = runner_mod.annotate_diff_with_line_numbers(full_diff)
            msg += f"\n\nHere is the unified diff for this PR:\n```diff\n{annotated}\n```"
        if prompt_suffix:
            msg += "\n\n" + prompt_suffix
        if runner_mod.logger.isEnabledFor(runner_mod.logging.DEBUG):
            runner_mod.logger.debug(
                "LLM request (single-shot) session=%s prompt=%s", session_id, msg
            )
        content = runner_mod.types.Content(role="user", parts=[runner_mod.types.Part(text=msg)])
        response_text = runner_mod._run_agent_and_collect_response(
            runner, session_service, session_id, content
        )
        return runner_mod._findings_from_response(response_text)

    def _attach_fingerprints_and_filter_findings(
        self,
        all_findings: list[runner_mod.FindingV1],
        provider,
        ignore_set: set[tuple[str, str]],
        resolved_body_set: set[tuple[str, str]],
        resolved_fp_set: set[tuple[str, str]],
    ) -> list[tuple[runner_mod.FindingV1, str]]:
        """
        Attach fingerprints to findings, filter by ignore/resolved sets.
        Mutates ignore_set (adds new keys). Returns to_post: list of (finding, fingerprint).
        """
        to_post: list[tuple[runner_mod.FindingV1, str]] = []
        unique_paths = list(dict.fromkeys(f.path for f in all_findings))
        file_lines_by_path = (
            runner_mod._get_file_lines_by_path(
                provider, self.owner, self.repo, self.head_sha, unique_paths
            )
            if self.head_sha
            else {}
        )
        for f in all_findings:
            body = runner_mod.finding_to_comment_body(f)
            body_hash = runner_mod.hashlib.sha256(body.encode()).hexdigest()
            fp = (
                runner_mod._fingerprint_for_finding(f, file_lines_by_path)
                if file_lines_by_path
                else ""
            )
            if runner_mod._should_skip_finding_for_dedup(
                f.path, body_hash, fp, ignore_set, resolved_body_set, resolved_fp_set
            ):
                continue
            if fp:
                ignore_set.add((f.path, fp))
            ignore_set.add((f.path, body_hash))
            to_post.append((f, fp))
        return to_post

    def _post_findings_and_summary(
        self,
        provider,
        incremental_base_sha: str,
        to_post: list[tuple[runner_mod.FindingV1, str]],
        cfg,
        llm_cfg,
        existing: list,
        full_diff: str = "",
    ) -> int:
        """
        Auto-resolve stale comments (if supported), then post inline comments.
        Returns successful_post_count.
        full_diff: used to set line_type (ADDED vs CONTEXT) so Bitbucket
        Server anchors comments correctly.
        """
        runner_mod._resolve_stale_comments_if_supported(
            provider,
            self.owner,
            self.repo,
            self.pr_number,
            existing,
            to_post,
            self.head_sha,
            self.dry_run,
        )
        if self.dry_run:
            return 0
        gate_outcome = runner_mod._compute_quality_gate_review_outcome(
            provider, self.owner, self.repo, self.pr_number, to_post, cfg
        )
        runner_mod._log_quality_gate_review_outcome("Full-review", gate_outcome)
        count = 0
        if to_post:
            if not self.head_sha:
                raise ValueError(
                    "head_sha is required when posting comments (dry_run=False). "
                    "Provide head_sha or use --dry-run to skip posting."
                )
            count = runner_mod._post_inline_comments(
                provider,
                self.owner,
                self.repo,
                self.pr_number,
                self.head_sha,
                incremental_base_sha,
                to_post,
                cfg,
                llm_cfg,
                full_diff=full_diff,
            )
        if self.head_sha and provider.capabilities().omit_fingerprint_marker_in_body:
            planned = len(to_post)
            include_marker = planned == 0 or count == planned
            runner_mod._post_omit_marker_pr_summary_comment(
                provider,
                self.owner,
                self.repo,
                self.pr_number,
                cfg,
                llm_cfg,
                self.head_sha,
                incremental_base_sha,
                findings_planned=planned,
                successful_inline_posts=count,
                gate_outcome=gate_outcome,
                include_run_marker=include_marker,
            )
        runner_mod._maybe_submit_review_decision(
            provider,
            self.owner,
            self.repo,
            self.pr_number,
            self.head_sha,
            self.dry_run,
            cfg,
            gate_outcome=gate_outcome,
        )
        return count

    def _record_observability_and_build_result(
        self,
        trace_id: str,
        start_time: float,
        run_handle,
        paths: list,
        all_findings: list[runner_mod.FindingV1],
        successful_post_count: int,
        to_post: list[tuple[runner_mod.FindingV1, str]],
        context_brief_attached: bool = False,
    ) -> list[runner_mod.FindingV1]:
        """
        Emit run_complete log and observability.finish_run, then return the list of findings posted.
        """
        _duration_ms = (runner_mod.time.perf_counter() - start_time) * 1000
        runner_mod._log_run_complete(
            trace_id,
            self.owner,
            self.repo,
            self.pr_number,
            files_count=len(paths),
            findings_count=len(all_findings),
            posts_count=successful_post_count,
            duration_ms=_duration_ms,
        )
        runner_mod.observability.finish_run(
            run_handle,
            self.owner,
            self.repo,
            self.pr_number,
            files_count=len(paths),
            findings_count=len(all_findings),
            posts_count=successful_post_count,
            duration_seconds=_duration_ms / 1000.0,
            context_brief_attached=context_brief_attached,
        )
        return [f for f, _ in to_post]

    @staticmethod
    def _print_findings_summary(
        print_findings: bool, to_post: list[tuple[runner_mod.FindingV1, str]]
    ) -> None:
        if not print_findings:
            return
        if to_post:
            print("\n" + "=" * 60)
            print(f"Code Review Findings ({len(to_post)} total)")
            print("=" * 60)
            for f, _ in to_post:
                line_info = (
                    f"{f.line}"
                    if not f.end_line or f.end_line == f.line
                    else f"{f.line}-{f.end_line}"
                )
                print(f"[{f.severity.upper()}] {f.path}:{line_info}")
                print(f"Message: {f.get_body()}")
                if f.suggested_patch:
                    print("-" * 40)
                    print("Suggested Patch:")
                    print(f.suggested_patch)
                print("=" * 60)
            print()
        else:
            print("No findings to post.")

    @staticmethod
    def _log_post_counts(dry_run: bool, planned_count: int, successful_post_count: int) -> None:
        if dry_run:
            runner_mod.logger.info("Dry run: would post %d comment(s)", planned_count)
        else:
            runner_mod.logger.info("Posted %d comment(s)", successful_post_count)

    @staticmethod
    def _filter_findings_to_pr_paths(
        findings: list[runner_mod.FindingV1], paths: list[str]
    ) -> list[runner_mod.FindingV1]:
        if not paths:
            return findings
        allowed_normalized = {runner_mod._normalize_path_for_anchor(p) for p in paths}
        filtered_findings: list[runner_mod.FindingV1] = []
        for finding in findings:
            norm_path = runner_mod._normalize_path_for_anchor(finding.path or "")
            if norm_path in allowed_normalized:
                filtered_findings.append(finding)
            elif runner_mod.logger.isEnabledFor(runner_mod.logging.DEBUG):
                runner_mod.logger.debug(
                    "Dropping finding for path not in diff: %s (normalized=%s, allowed=%s)",
                    finding.path,
                    norm_path,
                    sorted(allowed_normalized),
                )
        return filtered_findings

    @staticmethod
    def _filter_findings_to_visible_diff_lines(
        findings: list[runner_mod.FindingV1], full_diff: str
    ) -> list[runner_mod.FindingV1]:
        if not full_diff:
            return findings
        visible_lines = runner_mod._diff_visible_new_lines(full_diff)
        if not visible_lines:
            return findings
        line_filtered: list[runner_mod.FindingV1] = []
        for finding in findings:
            norm_path = runner_mod._normalize_path_for_anchor(finding.path or "")
            if (norm_path, finding.line) in visible_lines:
                line_filtered.append(finding)
            else:
                runner_mod.logger.debug(
                    "Dropping finding for line not visible in diff: %s:%d",
                    finding.path,
                    finding.line,
                )
        return line_filtered

    def _filter_findings_by_diff_scope(
        self, findings: list[runner_mod.FindingV1], paths: list[str], full_diff: str
    ) -> list[runner_mod.FindingV1]:
        path_filtered = self._filter_findings_to_pr_paths(findings, paths)
        relocated = runner_mod._relocate_findings_by_anchor(path_filtered, full_diff)
        line_filtered = self._filter_findings_to_visible_diff_lines(relocated, full_diff)
        contradiction_filtered = runner_mod._filter_obviously_contradicted_findings(
            line_filtered, full_diff
        )
        patched = runner_mod._validate_suggested_patches(contradiction_filtered, full_diff)
        return runner_mod._filter_self_retracted_finding_messages(patched)

    def _build_pr_url(self, cfg) -> str:
        base_url = cfg.url.rstrip("/")
        if cfg.provider == "github":
            return f"{base_url}/{self.owner}/{self.repo}/pull/{self.pr_number}"
        if cfg.provider == "gitlab":
            return f"{base_url}/{self.owner}/{self.repo}/-/merge_requests/{self.pr_number}"
        if cfg.provider == "bitbucket":
            return f"https://bitbucket.org/{self.owner}/{self.repo}/pull-requests/{self.pr_number}"
        if cfg.provider == "bitbucket_server":
            return (
                f"{base_url}/projects/{self.owner}/repos/{self.repo}/pull-requests/{self.pr_number}"
            )
        return f"{base_url}/{self.owner}/{self.repo}/pulls/{self.pr_number}"

    def _load_commit_messages(self, provider, need_commits: bool) -> list[str]:
        raw = (
            provider.get_pr_commit_messages(self.owner, self.repo, self.pr_number)
            if need_commits
            else []
        )
        return raw if isinstance(raw, list) else []

    def _build_prompt_suffix(
        self,
        provider,
        cfg,
        ctx_cfg,
        app_cfg,
        pr_info_for_metadata,
        full_diff: str,
        remaining_tokens: int,
    ) -> tuple[list[object], str | None, str]:
        need_commits = app_cfg.include_commit_messages_in_prompt or ctx_cfg.enabled
        commit_messages = self._load_commit_messages(provider, need_commits)
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
            try:
                context_brief = runner_mod.build_context_brief_for_pr(ctx_cfg, cfg, refs, full_diff)
            except runner_mod.ContextAwareFatalError:
                runner_mod.logger.exception("Context-aware fetch or distillation failed")
                raise
        prompt_suffix = runner_mod._format_review_prompt_supplement(
            context_brief=context_brief,
            commit_messages=commit_messages,
            include_commit_messages=app_cfg.include_commit_messages_in_prompt,
            remaining_tokens=remaining_tokens,
        )
        return (refs, context_brief, prompt_suffix)

    def _decision_only_try_skip_when_bot_not_blocking(
        self,
        provider,
        app_cfg,
        trace_id: str,
        start_time: float,
        run_handle,
    ) -> list[runner_mod.FindingV1] | None:
        if not (
            app_cfg.review_decision_only_skip_if_bot_not_blocking
            and runner_mod.event_allows_decision_only_skip_when_bot_not_blocking(
                self._event_context
            )
        ):
            return None
        caps = provider.capabilities()
        if not caps.supports_bot_blocking_state_query:
            return None
        if provider.get_bot_blocking_state(self.owner, self.repo, self.pr_number) != "NOT_BLOCKING":
            return None
        runner_mod.logger.info(
            "Review-decision-only: skipping quality gate "
            "(bot not blocking; "
            "CODE_REVIEW_REVIEW_DECISION_ONLY_SKIP_IF_BOT_NOT_BLOCKING=1, "
            "comment_id present)."
        )
        return self._record_observability_and_build_result(
            trace_id,
            start_time,
            run_handle,
            paths=[],
            all_findings=[],
            successful_post_count=0,
            to_post=[],
        )

    def _decision_only_try_skip_when_event_actor_is_bot(
        self,
        provider,
        trace_id: str,
        start_time: float,
        run_handle,
    ) -> list[runner_mod.FindingV1] | None:
        """Skip review-decision-only runs triggered by the bot's own comment activity."""
        ctx = self._event_context
        if ctx is None:
            return None
        actor_login = (ctx.actor_login or "").strip()
        actor_id = (ctx.actor_id or "").strip()
        if not actor_login and (not actor_id):
            return None
        caps = provider.capabilities()
        if not caps.supports_bot_attribution_identity_query:
            return None
        bot_id = provider.get_bot_attribution_identity(self.owner, self.repo, self.pr_number)
        if not runner_mod._reply_added_event_authored_by_bot(ctx, bot_id):
            return None
        if (ctx.comment_id or "").strip():
            runner_mod.observability.record_reply_dismissal_outcome("skipped_bot_author")
        runner_mod.logger.info(
            "Review-decision-only: skipping bot-authored webhook event "
            "(actor_login=%r actor_id=%r comment_id=%r source=%r)",
            actor_login,
            actor_id,
            (ctx.comment_id or "").strip(),
            (ctx.source or "").strip(),
        )
        return self._record_observability_and_build_result(
            trace_id,
            start_time,
            run_handle,
            paths=[],
            all_findings=[],
            successful_post_count=0,
            to_post=[],
        )

    def _validate_context_sources_or_raise(
        self,
        ctx_cfg,
        cfg,
        run_handle,
        start_time: float,
    ) -> None:
        """Validate context-aware sources when enabled and finish observability on fatal config."""
        if not ctx_cfg.enabled:
            return
        try:
            runner_mod.validate_context_aware_sources(ctx_cfg, cfg)
        except runner_mod.ContextAwareFatalError as e:
            runner_mod.logger.error("Context-aware review configuration error: %s", e)
            runner_mod.observability.finish_run(
                run_handle,
                self.owner,
                self.repo,
                self.pr_number,
                files_count=0,
                findings_count=0,
                posts_count=0,
                duration_seconds=runner_mod.time.perf_counter() - start_time,
            )
            raise

    @staticmethod
    def _log_review_scope_fetch(incremental_base_sha: str, head_sha: str, paths: list[str]) -> None:
        """Emit a concise log line describing the fetched review scope."""
        if incremental_base_sha:
            runner_mod.logger.info(
                "Fetched incremental diff base=%s head=%s, %d file(s) to review",
                incremental_base_sha[:12],
                (head_sha or "")[:12],
                len(paths),
            )
            return
        runner_mod.logger.info("Fetched diff, %d file(s) to review", len(paths))

    @staticmethod
    def _build_review_execution_mode(
        full_diff: str, diff_budget: int, incremental_base_sha: str, paths: list[str]
    ) -> tuple[bool, bool, dict[str, str] | None]:
        """Return execution mode flags plus an optional embedded per-file diff map."""
        use_file_by_file = runner_mod._estimate_tokens(full_diff) > diff_budget
        use_embedded_file_diffs = bool(incremental_base_sha and use_file_by_file)
        diff_by_path = (
            {path: runner_mod.unified_diff_for_path(full_diff, path) for path in paths}
            if use_embedded_file_diffs
            else None
        )
        return (use_file_by_file, use_embedded_file_diffs, diff_by_path)

    @staticmethod
    def _log_review_execution_mode(
        use_file_by_file: bool, use_embedded_file_diffs: bool, paths: list[str]
    ) -> None:
        """Log whether this run is single-shot, tool-based, or embedded file-by-file."""
        if not use_file_by_file:
            runner_mod.logger.info("Running agent (single shot)")
            return
        if use_embedded_file_diffs:
            runner_mod.logger.info(
                "Running agent on %d file(s) (embedded file-by-file, incremental scope)",
                len(paths),
            )
            return
        runner_mod.logger.info("Running agent on %d file(s) (file-by-file)", len(paths))

    def _decision_only_maybe_post_disagreed_thread_reply(
        self,
        provider,
        caps_rd,
        comment_id: str,
        verdict: runner_mod.ReplyDismissalVerdictV1,
    ) -> None:
        if not caps_rd.supports_review_thread_reply:
            runner_mod.logger.info(
                "Reply-dismissal disagreed: provider does not support thread replies"
            )
            return
        if self.dry_run:
            truncated = (verdict.reply_text or "")[:500]
            runner_mod.logger.info(
                "Dry-run: would post review-thread reply (truncated): %s", truncated
            )
            return
        try:
            provider.post_review_thread_reply(
                self.owner, self.repo, self.pr_number, comment_id, verdict.reply_text
            )
            runner_mod.logger.info(
                "Reply-dismissal disagreed: posted follow-up reply to comment_id=%s",
                comment_id,
            )
        except Exception as e:
            runner_mod.logger.warning("post_review_thread_reply failed: %s", e)

    def _decision_only_maybe_post_agreed_thread_reply(
        self,
        provider,
        caps_rd,
        comment_id: str,
    ) -> bool:
        if not caps_rd.supports_review_thread_reply:
            runner_mod.logger.info(
                "Reply-dismissal agreed: provider does not support thread replies; "
                "cannot persist accepted thread state"
            )
            return False
        if self.dry_run:
            runner_mod.logger.info(
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
            runner_mod.logger.info(
                "Reply-dismissal agreed: posted durable accepted-thread reply to comment_id=%s",
                comment_id,
            )
            return True
        except Exception as e:
            runner_mod.logger.warning("post agreed accepted-thread reply failed: %s", e)
            return False

    def _decision_only_maybe_resolve_agreed_thread(
        self,
        provider,
        caps_rd,
        comment_id: str,
        dctx: runner_mod.ReviewThreadDismissalContext,
    ) -> bool:
        if not caps_rd.supports_review_thread_resolution:
            return self._decision_only_maybe_post_agreed_thread_reply(provider, caps_rd, comment_id)
        if self.dry_run:
            runner_mod.logger.info(
                "Dry-run: would resolve review thread stable_id=%s thread_id=%s",
                dctx.gate_exclusion_stable_id,
                (dctx.thread_id or "").strip(),
            )
            return False
        try:
            provider.resolve_review_thread(
                self.owner, self.repo, self.pr_number, dctx, comment_id
            )
            runner_mod.logger.info(
                "Reply-dismissal agreed: resolved review thread stable_id=%s thread_id=%s",
                dctx.gate_exclusion_stable_id,
                (dctx.thread_id or "").strip(),
            )
            return True
        except Exception as e:
            runner_mod.logger.warning("resolve_review_thread failed: %s", e)
            return self._decision_only_maybe_post_agreed_thread_reply(provider, caps_rd, comment_id)

    def _reply_dismissal_comment_id_or_none(self, app_cfg) -> str | None:
        """Return event comment id when reply-dismissal should run, else ``None``."""
        ctx = self._event_context
        comment_id = (ctx.comment_id or "").strip() if ctx else ""
        if not app_cfg.reply_dismissal_enabled:
            if comment_id:
                runner_mod.logger.info(
                    "Reply-dismissal disabled: "
                    "CODE_REVIEW_REPLY_DISMISSAL_ENABLED is explicitly false; "
                    "skipping LLM for comment_id=%s",
                    comment_id,
                )
            return None
        if app_cfg.reply_dismissal_enabled and ctx is not None and (ctx.comment_id or "").strip():
            return ctx.comment_id.strip()
        runner_mod.logger.info(
            "Reply-dismissal enabled but not run: requires "
            "CODE_REVIEW_EVENT_COMMENT_ID; "
            "got comment_id=%r ctx_present=%s",
            comment_id or "",
            ctx is not None,
        )
        return None

    def _reply_dismissal_precheck(
        self, provider, comment_id: str
    ) -> tuple[runner_mod.BotAttributionIdentity, runner_mod.ReviewThreadDismissalContext] | None:
        """Return bot identity and dismissal context when reply-dismissal can proceed."""
        ctx = self._event_context
        if ctx is None:
            runner_mod.logger.info("Reply-dismissal skipped: no event context present")
            return None
        runner_mod.logger.info(
            "Reply-dismissal candidate: loading thread context for comment_id=%s",
            comment_id,
        )
        bot_id = provider.get_bot_attribution_identity(self.owner, self.repo, self.pr_number)
        if runner_mod._reply_added_event_authored_by_bot(ctx, bot_id):
            runner_mod.observability.record_reply_dismissal_outcome("skipped_bot_author")
            runner_mod.logger.info(
                "Reply-dismissal skipped: reply_added actor matches bot "
                "(actor_login=%r actor_id=%r)",
                (ctx.actor_login or "").strip(),
                (ctx.actor_id or "").strip(),
            )
            return None
        caps_rd = provider.capabilities()
        if not caps_rd.supports_review_thread_dismissal_context:
            runner_mod.observability.record_reply_dismissal_outcome("skipped_no_capability")
            runner_mod.logger.info(
                "Reply-dismissal skipped: provider does not support review-thread context"
            )
            return None
        dctx = provider.get_review_thread_dismissal_context(
            self.owner, self.repo, self.pr_number, comment_id
        )
        if dctx is None or len(dctx.entries) < 2:
            runner_mod.observability.record_reply_dismissal_outcome("skipped_insufficient_thread")
            runner_mod.logger.info(
                "Reply-dismissal skipped: insufficient thread context "
                "for comment_id=%s (entries=%s)",
                comment_id,
                len(dctx.entries) if dctx is not None else 0,
            )
            return None
        existing_bot_reply = runner_mod._reply_dismissal_existing_bot_reply_after_trigger(
            dctx, bot_id, comment_id
        )
        if existing_bot_reply is not None:
            runner_mod.observability.record_reply_dismissal_outcome("skipped_already_replied")
            runner_mod.logger.info(
                "Reply-dismissal skipped: triggering comment_id=%s already has "
                "a later bot reply in thread (comment_id=%s)",
                comment_id,
                (existing_bot_reply.comment_id or "").strip(),
            )
            return None
        runner_mod.logger.info(
            "Reply-dismissal thread loaded: comment_id=%s entries=%d stable_id=%s thread_id=%s",
            comment_id,
            len(dctx.entries),
            dctx.gate_exclusion_stable_id,
            (dctx.thread_id or "").strip(),
        )
        return (bot_id, dctx)

    @staticmethod
    def _reply_dismissal_parse_verdict(
        raw_verdict: str,
    ) -> runner_mod.ReplyDismissalVerdictV1 | None:
        """Parse reply-dismissal LLM output and log a helpful truncated warning on failure."""
        verdict = runner_mod.reply_dismissal_verdict_from_llm_text(raw_verdict)
        if verdict is not None:
            return verdict
        runner_mod.observability.record_reply_dismissal_outcome("parse_failed")
        snippet = (raw_verdict or "").strip()
        if len(snippet) > 1500:
            snippet = snippet[:1500] + "…"
        runner_mod.logger.warning(
            "Reply-dismissal LLM output could not be parsed as "
            "ReplyDismissalVerdictV1; enable DEBUG for full request/response. "
            "Raw (truncated): %r",
            snippet or "(empty)",
        )
        return None

    def _reply_dismissal_diff_context(
        self,
        provider,
        dctx: runner_mod.ReviewThreadDismissalContext,
    ) -> str:
        path = (dctx.path or "").strip()
        if not path:
            return ""
        if not provider.capabilities().supports_lightweight_pr_diff_for_file:
            return ""
        try:
            return runner_mod._reply_dismissal_diff_context_for_thread(
                provider.get_pr_diff_for_file(self.owner, self.repo, self.pr_number, path), dctx
            )
        except Exception as e:
            runner_mod.logger.warning(
                "Reply-dismissal diff context unavailable for path=%s line=%s: %s",
                path,
                int(dctx.line or 0),
                e,
            )
            return ""

    def _reply_dismissal_run_llm_and_parse(
        self, user_msg: str
    ) -> runner_mod.ReplyDismissalVerdictV1 | None:
        try:
            raw_verdict = runner_mod._run_reply_dismissal_llm(user_msg)
        except Exception as e:
            runner_mod.logger.warning("Reply-dismissal LLM run failed: %s", e)
            runner_mod.observability.record_reply_dismissal_outcome("llm_error")
            return None
        runner_mod.logger.info(
            "Reply-dismissal LLM completed: response_chars=%d",
            len((raw_verdict or "").strip()),
        )
        if runner_mod.logger.isEnabledFor(runner_mod.logging.DEBUG):
            runner_mod.logger.debug(
                "Reply-dismissal raw LLM response: %s",
                runner_mod._reply_dismissal_response_log_snippet(raw_verdict, limit=4000),
            )
        return self._reply_dismissal_parse_verdict(raw_verdict)

    def _reply_dismissal_excluded_gate_ids_from_verdict(
        self,
        provider,
        comment_id: str,
        dctx: runner_mod.ReviewThreadDismissalContext,
        verdict: runner_mod.ReplyDismissalVerdictV1,
    ) -> frozenset[str]:
        if verdict.verdict == "agreed":
            runner_mod.observability.record_reply_dismissal_outcome("agreed")
            persisted = self._decision_only_maybe_resolve_agreed_thread(
                provider, provider.capabilities(), comment_id, dctx
            )
            if persisted:
                runner_mod.logger.info(
                    "Reply-dismissal agreed; excluding gate stable_id=%s",
                    dctx.gate_exclusion_stable_id,
                )
                return frozenset({dctx.gate_exclusion_stable_id})
            runner_mod.logger.info(
                "Reply-dismissal agreed but SCM persistence failed; "
                "keeping gate stable_id=%s in quality gate",
                dctx.gate_exclusion_stable_id,
            )
            return frozenset()
        if verdict.verdict == "disagreed":
            runner_mod.observability.record_reply_dismissal_outcome("disagreed")
            self._decision_only_maybe_post_disagreed_thread_reply(
                provider, provider.capabilities(), comment_id, verdict
            )
        return frozenset()

    def _decision_only_reply_dismissal_excluded_gate_ids(
        self,
        provider,
        app_cfg,
        trace_id: str,
    ) -> frozenset[str]:
        """Stable ids to exclude from the quality gate after optional reply-dismissal LLM."""
        comment_id = self._reply_dismissal_comment_id_or_none(app_cfg)
        if comment_id is None:
            return frozenset()
        precheck = self._reply_dismissal_precheck(provider, comment_id)
        if precheck is None:
            return frozenset()
        bot_id, dctx = precheck
        scm_reason = runner_mod._reply_dismissal_scm_already_addressed_reason(dctx)
        if scm_reason:
            runner_mod.observability.record_reply_dismissal_outcome("skipped_scm_already_addressed")
            runner_mod.logger.info(
                "Reply-dismissal skipped LLM: SCM already indicates thread "
                "addressed (reason=%s stable_id=%s comment_id=%s)",
                scm_reason,
                dctx.gate_exclusion_stable_id,
                comment_id,
            )
            return frozenset({dctx.gate_exclusion_stable_id})
        diff_context = self._reply_dismissal_diff_context(provider, dctx)
        user_msg = runner_mod._format_reply_dismissal_user_message(
            dctx, bot_id, comment_id, diff_context
        )
        runner_mod.logger.info(
            "Reply-dismissal sending thread to LLM: comment_id=%s "
            "entries=%d stable_id=%s path=%s line=%s diff_context=%s",
            comment_id,
            len(dctx.entries),
            dctx.gate_exclusion_stable_id,
            (dctx.path or "").strip(),
            int(dctx.line or 0),
            "yes" if diff_context else "no",
        )
        verdict = self._reply_dismissal_run_llm_and_parse(user_msg)
        if verdict is None:
            return frozenset()
        runner_mod.logger.info(
            "reply_dismissal_verdict trace_id=%s verdict=%s pr=%s/%s#%s",
            trace_id,
            verdict.verdict,
            self.owner,
            self.repo,
            self.pr_number,
        )
        return self._reply_dismissal_excluded_gate_ids_from_verdict(
            provider, comment_id, dctx, verdict
        )

    def _run_review_decision_only(
        self, trace_id: str, start_time: float, run_handle, cfg, provider
    ) -> list[runner_mod.FindingV1]:
        """Recompute quality-gate counts from SCM state and submit review decision only."""
        pr_url = self._build_pr_url(cfg)
        runner_mod.logger.info(
            "Review-decision-only run for %s/%s PR %s (provider=%s) URL: %s",
            self.owner,
            self.repo,
            self.pr_number,
            cfg.provider,
            pr_url,
        )
        print(f"Review-decision-only for PR: {pr_url}")
        runner_mod._log_review_decision_event_if_present(self._event_context)
        app_cfg = runner_mod.get_code_review_app_config()
        skip_result = self._determine_skip_reason(provider, cfg, trace_id, start_time, run_handle)
        if skip_result is not None:
            return skip_result
        skip_bot_event = self._decision_only_try_skip_when_event_actor_is_bot(
            provider, trace_id, start_time, run_handle
        )
        if skip_bot_event is not None:
            return skip_bot_event
        skip_early = self._decision_only_try_skip_when_bot_not_blocking(
            provider, app_cfg, trace_id, start_time, run_handle
        )
        if skip_early is not None:
            return skip_early
        head_hint = runner_mod._head_sha_hint_for_decision_only(self.head_sha)
        head_sha = runner_mod._resolve_head_sha_for_review_decision_submission(
            provider, self.owner, self.repo, self.pr_number, head_hint
        )
        if not head_sha and (not self.dry_run):
            runner_mod.logger.warning(
                "Review-decision-only: head_sha missing after provider lookup; "
                "submit_review_decision may omit commit id for some SCMs."
            )
        excluded_gate = self._decision_only_reply_dismissal_excluded_gate_ids(
            provider, app_cfg, trace_id
        )
        gate_outcome = runner_mod._compute_quality_gate_review_outcome(
            provider,
            self.owner,
            self.repo,
            self.pr_number,
            [],
            cfg,
            excluded_gate_stable_ids=excluded_gate if excluded_gate else None,
        )
        runner_mod._log_quality_gate_review_outcome("Review-decision-only", gate_outcome)
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
        return self._record_observability_and_build_result(
            trace_id,
            start_time,
            run_handle,
            paths=[],
            all_findings=[],
            successful_post_count=0,
            to_post=[],
        )

    def _resolve_empty_scope_submission_head_sha(
        self,
        provider,
        head_sha: str,
        pr_info_for_metadata: runner_mod.Any,
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

    def _maybe_finish_empty_scope_review(
        self,
        provider,
        cfg,
        head_sha: str,
        trace_id: str,
        start_time: float,
        run_handle,
        paths: list[str],
        pr_info_for_metadata: runner_mod.Any,
    ) -> list[runner_mod.FindingV1] | None:
        """Handle the empty-review-scope early return, including review-decision refresh."""
        if paths:
            return None
        runner_mod.logger.info("No files to review")
        if bool(getattr(cfg, "review_decision_enabled", False)):
            runner_mod.logger.info(
                "Recomputing PR review decision from unresolved SCM state "
                "despite empty review scope"
            )
            gate_outcome = runner_mod._compute_quality_gate_review_outcome(
                provider, self.owner, self.repo, self.pr_number, [], cfg
            )
            runner_mod._log_quality_gate_review_outcome("Empty-scope refresh", gate_outcome)
            submission_head_sha = self._resolve_empty_scope_submission_head_sha(
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
        return self._record_observability_and_build_result(
            trace_id, start_time, run_handle, paths, [], 0, []
        )

    def _log_context_aware_prompt_inputs(
        self, refs: list[runner_mod.Any], context_brief: str
    ) -> None:
        """Log context-aware prompt supplements only when present."""
        if refs:
            runner_mod.logger.info(
                "context_aware: extracted %d reference(s) from PR text", len(refs)
            )
        if context_brief:
            runner_mod.logger.info("context_aware: distilled context attached to review prompt")

    def _run_standard_review(
        self,
        trace_id: str,
        start_time: float,
        run_handle,
        cfg,
        llm_cfg,
        provider,
        app_cfg,
    ) -> list[runner_mod.FindingV1]:
        """Execute the full non-decision-only review path."""
        pr_url = self._build_pr_url(cfg)
        runner_mod.logger.info(
            "Reviewing %s/%s PR %s (provider=%s) URL: %s",
            self.owner,
            self.repo,
            self.pr_number,
            cfg.provider,
            pr_url,
        )
        print(f"Starting review for PR: {pr_url}")
        skip_result = self._determine_skip_reason(provider, cfg, trace_id, start_time, run_handle)
        if skip_result is not None:
            return skip_result
        (
            existing,
            existing_dicts,
            ignore_set,
            _resolved_comments,
            resolved_body_set,
            resolved_fp_set,
        ) = self._load_existing_comments_and_markers(provider)
        incremental_base_sha = self._incremental_base_sha(cfg, self.head_sha)
        idempotency_result = self._compute_idempotency_and_maybe_short_circuit(
            cfg,
            llm_cfg,
            existing_dicts,
            trace_id,
            start_time,
            run_handle,
            incremental_base_sha=incremental_base_sha,
        )
        if idempotency_result is not None:
            runner_mod.logger.info(
                "Skipping run (idempotent: same review range/config already reviewed)"
            )
            return idempotency_result
        ctx_cfg = runner_mod.get_context_aware_config()
        self._validate_context_sources_or_raise(ctx_cfg, cfg, run_handle, start_time)
        pr_info_for_metadata = provider.get_pr_info(self.owner, self.repo, self.pr_number)
        _, paths, full_diff, incremental_base_sha = self._fetch_review_files_and_diffs(
            provider, cfg
        )
        paths = self._build_ignore_set_and_filter_files(paths)
        self._log_review_scope_fetch(incremental_base_sha, self.head_sha, paths)
        empty_scope_result = self._maybe_finish_empty_scope_review(
            provider,
            cfg,
            self.head_sha,
            trace_id,
            start_time,
            run_handle,
            paths,
            pr_info_for_metadata,
        )
        if empty_scope_result is not None:
            return empty_scope_result
        if not self.dry_run:
            runner_mod._maybe_post_started_review_comment(
                provider, self.owner, self.repo, self.pr_number, pr_info_for_metadata, paths
            )
        _, review_standards = self._detect_languages_for_files(paths)
        context_window = runner_mod.get_context_window()
        diff_budget = int(context_window * runner_mod.DIFF_TOKEN_BUDGET_RATIO)
        remaining_prompt_tokens = max(0, context_window - diff_budget)
        refs, context_brief, prompt_suffix = self._build_prompt_suffix(
            provider,
            cfg,
            ctx_cfg,
            app_cfg,
            pr_info_for_metadata,
            full_diff,
            remaining_prompt_tokens,
        )
        self._log_context_aware_prompt_inputs(refs, context_brief)
        use_file_by_file, use_embedded_file_diffs, diff_by_path = self._build_review_execution_mode(
            full_diff, diff_budget, incremental_base_sha, paths
        )
        self._log_review_execution_mode(use_file_by_file, use_embedded_file_diffs, paths)
        session_id, session_service, runner = self._create_agent_and_runner(
            provider,
            review_standards,
            use_file_by_file=use_file_by_file,
            disable_tools=True if use_embedded_file_diffs else None,
            context_brief_attached=bool(context_brief and "<context>" in prompt_suffix),
        )
        all_findings = self._run_agent_and_collect_findings(
            runner,
            session_service,
            session_id,
            paths,
            use_file_by_file,
            full_diff=full_diff,
            prompt_suffix=prompt_suffix,
            diff_by_path=diff_by_path,
        )
        all_findings = self._filter_findings_by_diff_scope(all_findings, paths, full_diff)
        to_post = self._attach_fingerprints_and_filter_findings(
            all_findings,
            provider,
            ignore_set,
            resolved_body_set,
            resolved_fp_set,
        )
        runner_mod.logger.info(
            "Agent returned %d finding(s), %d to post after filtering",
            len(all_findings),
            len(to_post),
        )
        self._print_findings_summary(self.print_findings, to_post)
        successful_post_count = self._post_findings_and_summary(
            provider,
            incremental_base_sha,
            to_post,
            cfg,
            llm_cfg,
            existing,
            full_diff=full_diff,
        )
        self._log_post_counts(self.dry_run, len(to_post), successful_post_count)
        return self._record_observability_and_build_result(
            trace_id,
            start_time,
            run_handle,
            paths,
            all_findings,
            successful_post_count,
            to_post,
            context_brief_attached=bool(context_brief and "<context>" in prompt_suffix),
        )

    def run(self) -> list[runner_mod.FindingV1]:
        """
        Execute the full review flow. Returns list of findings that were posted
        (or would be posted if dry_run).
        """
        trace_id = str(runner_mod.uuid.uuid4())
        start_time = runner_mod.time.perf_counter()
        run_handle = runner_mod.observability.start_run(trace_id)
        cfg, llm_cfg, provider = self._load_config_and_provider()
        app_cfg = runner_mod.get_code_review_app_config()
        decision_only = bool(self._review_decision_only) or bool(app_cfg.review_decision_only)
        if decision_only:
            return self._run_review_decision_only(trace_id, start_time, run_handle, cfg, provider)
        return self._run_standard_review(
            trace_id, start_time, run_handle, cfg, llm_cfg, provider, app_cfg
        )
