# Implementation Checklist

Derived from the AI Code Review Agent plan. Mark items with `[x]` when complete.

---

## Phase 1: Project Foundation and Google ADK Agent

### 1.1 Project Structure
- [x] `pyproject.toml` (Python 3.10+, google-adk)
- [x] `src/code_review/__init__.py`
- [x] `src/code_review/__main__.py` — CLI invokes Runner
- [x] `src/code_review/agent/__init__.py`
- [x] `src/code_review/agent/agent.py`
- [x] `src/code_review/agent/tools/__init__.py`
- [x] `src/code_review/agent/tools/gitea_tools.py` (plan: scm_tools.py)
- [x] `src/code_review/agent/tools/review_helpers.py`
- [x] `src/code_review/providers/base.py`
- [x] `src/code_review/providers/gitea.py`
- [x] `src/code_review/standards/detector.py`
- [x] `src/code_review/standards/prompts/` (base + per-language)
- [x] `src/code_review/models.py`
- [x] `src/code_review/config.py`
- [x] `src/code_review/runner.py`
- [x] `src/code_review/schemas/findings.py`
- [x] `src/code_review/diff/parser.py`
- [x] `src/code_review/diff/position.py`
- [x] `src/code_review/diff/fingerprint.py`
- [x] `.env.example`
- [x] `README.md`

### 1.2 Config and Model Factory
- [x] `config.py` — SCMConfig, LLMConfig (Pydantic Settings)
- [x] SCM env vars: SCM_PROVIDER, SCM_URL, SCM_TOKEN, SCM_OWNER, SCM_REPO, SCM_PR_NUM, SCM_HEAD_SHA, SCM_BASE_SHA
- [x] SCM_PROVIDER includes `github`
- [x] LLM env vars: LLM_PROVIDER, LLM_MODEL, LLM_CONTEXT_WINDOW, LLM_MAX_OUTPUT_TOKENS
- [x] `LLM_DISABLE_TOOL_CALLS` (plan: LLM_DISABLE_TOOL_CALLS) for debug
- [x] `models.py` — get_configured_model() for gemini, openai, anthropic, ollama, vertex
- [x] `models.py` — get_context_window(), get_max_output_tokens()
- [x] Temperature 0 or very low
- [x] Per-provider timeout and retry policy (LLM_TIMEOUT, LLM_MAX_RETRIES in config)

### 1.3 Provider Interface (base.py)
- [x] `get_pr_diff(owner, repo, pr_number) -> str`
- [x] `get_pr_diff_for_file(owner, repo, pr_number, path) -> str`
- [x] `get_file_content(owner, repo, ref, path) -> str`
- [x] `get_file_lines(owner, repo, ref, path, start_line, end_line) -> str`
- [x] `get_pr_files(owner, repo, pr_number) -> list[FileInfo]`
- [x] `post_review_comments(owner, repo, pr_number, comments) -> void`
- [x] `get_existing_review_comments(owner, repo, pr_number) -> list[ReviewComment]`
- [x] `resolve_comment(owner, repo, comment_id) -> void`
- [x] `unresolve_comment(owner, repo, comment_id) -> void` (optional)
- [x] `post_pr_summary_comment(owner, repo, pr_number, body) -> void`
- [x] `ProviderCapabilities(resolvable_comments, supports_suggestions, ...)`

### 1.4 Gitea Provider
- [x] GET /repos/.../pulls/{index}.diff
- [x] GET /repos/.../contents/{path}?ref={ref}
- [x] GET /repos/.../pulls/{index}/files
- [x] POST /repos/.../pulls/{index}/reviews (body + comments)
- [x] GET /repos/.../pulls/{index}/comments

### 1.5 Diff Parser
- [x] `parse_unified_diff()` — hunks, old/new line maps
- [x] `DiffHunk(path, old_start, old_count, new_start, new_count, lines)`
- [x] Commentable positions: map `(path, line_in_new_file)` to hunk index and API-specific coordinates
- [ ] Provider adapters convert internal representation to SCM API payload

### 1.6 ADK Tools (agent tools)
- [x] `get_pr_diff`
- [x] `get_pr_diff_for_file`
- [x] `get_file_content`
- [x] `get_file_lines`
- [x] `get_pr_files`
- [x] `post_review_comment` (plan: runner handles posting; current: agent posts)
- [x] `get_existing_review_comments` (plan: runner-only; current: agent has it)
- [x] `detect_language_context` — LLM fallback for ambiguous detection

### 1.7 Agent Definition (agent.py)
- [x] Agent uses get_configured_model()
- [x] Instruction includes review criteria
- [x] Agent returns structured findings only (no post/resolve tools) — `findings_only=True` default
- [x] Agent has get_pr_diff_for_file, get_file_lines, detect_language_context
- [x] Agent does NOT have post_review_comment, get_existing_review_comments (in findings_only mode)

### 1.8 Runner
- [x] Creates provider, detects language, gets review standards
- [x] Creates agent, runs Runner.run()
- [x] Runner fetches existing comments and builds ignore list (not agent)
- [x] Runner invokes agent with pre-chunked diff when over token budget
- [x] Runner parses agent structured output (FindingV1)
- [x] Runner filters findings against ignore list
- [x] Runner posts via provider (not agent)
- [x] Token budget check via LLM_CONTEXT_WINDOW
- [x] File-by-file loop when diff exceeds threshold
- [x] Structured output validation; re-ask on parse failure; fail gracefully (invalid items skipped)

### 1.9 Schemas (FindingV1)
- [x] path, line, end_line, severity, code, message, anchor
- [x] version
- [x] body (alias or mapping)
- [x] category
- [x] fingerprint_hint (code_span or anchor_text)

### 1.10 Language/Framework Detector
- [x] Extension → language (.py, .js, .ts, .go, .java, .c, .cpp, etc.)
- [x] Path signals → framework (requirements.txt, package.json, go.mod, pom.xml, etc.)
- [x] detect_from_paths(paths)
- [x] detect_from_paths_and_content(paths, content_by_path)
- [ ] Confidence as 0.0–1.0 with thresholds
- [ ] Monorepo mode: detect per file and per folder root

### 1.11 Prompts
- [x] Base prompt: role, categories, severity levels, comment format
- [x] Per-language: Python, JS/TS, Go, Java, C/C++
- [x] get_review_standards(language, framework)
- [x] Snippet policy: [Critical] diagnosis only; code snippets only for [Suggestion]
- [x] False positive control: category NeedsVerification for uncertainty

### 1.12 Repo-Content Safety (Section 1.9)
- [x] Max size per file (e.g. 16KB); truncate or reject
- [x] Explicit delimiter when truncated: `--- (truncated, max size exceeded)`
- [x] System instruction immutable; repo content cannot override tool rules

### 1.13 CLI (__main__.py)
- [x] `code-review review --owner --repo --pr [--head-sha]`
- [x] `--dry-run`, `--print-findings` — don't post
- [x] `--fail-on-critical` — exit non-zero if Critical findings

### Phase 1 Tests
- [x] `tests/providers/test_gitea.py` (mocked HTTP)
- [x] `tests/schemas/test_findings.py`
- [x] `tests/providers/test_safety.py` (repo content truncation)
- [x] `tests/test_runner_findings.py` (parse findings, ignore set)
- [x] `tests/diff/test_parser.py` (or test_diff_parser.py)
- [x] `tests/test_runner.py` (basic)
- [x] `tests/standards/test_detector.py` — extensions, frameworks, confidence
- [x] `tests/models/test_model_factory.py` — get_configured_model per provider
- [x] `tests/tools/test_scm_tools.py` — tools call provider correctly
- [x] `tests/test_runner.py` — ignore list, posts net-new (mocked run)

---

## Phase 2: Resolved Issue Tracking

### 2.1 Fingerprinting
- [ ] Fingerprint: (path, content_hash_of_surrounding_lines, issue_category)
- [ ] Anchor: normalized added/changed line text
- [ ] issue_code: stable ID per rule
- [ ] Compare against file content at head_sha; locate anchor; if not found, resolve

### 2.2 Runner Flow
- [ ] Runner fetches existing comments
- [ ] Runner builds fingerprints from old comments
- [ ] Runner runs agent; gets findings
- [ ] Runner compares old fingerprints vs new diff; auto-resolves stale via resolve_comment
- [ ] Runner filters new findings against manually-resolved ignore list
- [ ] Runner posts net-new via post_review_comments

### 2.3 Provider: resolve_comment
- [ ] Add resolve_comment to ProviderInterface
- [ ] Gitea: PATCH /repos/.../pulls/comments/{id} for resolved (or marker fallback)

### 2.4 Idempotency
- [ ] Idempotency key: {provider}/{owner}/{repo}/pr/{pr_number}/head/{head_sha}/agent/{version}/config/{hash}
- [ ] Storage: hidden marker in comment body or external cache (Redis, SQLite)
- [ ] Before review: check if key already processed; skip if yes

### 2.5 Manually Resolved / Ignore List
- [ ] get_existing_review_comments returns resolved: bool
- [ ] Build ignore fingerprint: (path, content_hash, message_body_hash)
- [ ] Don't post when (path, content_hash, body_hash) in ignore set
- [ ] Gitea resolved support or own lifecycle + marker

### 2.6 Force-Push / Rebase
- [ ] If inline post fails (position invalid), degrade to PR-level comment

### Phase 2 Tests
- [ ] tests/providers/test_resolved_tracking.py
- [ ] tests/providers/test_ignore_fingerprint.py
- [ ] tests/runner/test_idempotency.py
- [ ] tests/agent/test_ignore_list_integration.py

---

## Phase 3: Docker and CI Integration

### 3.1 Docker
- [ ] `docker/Dockerfile.agent` — Python + google-adk
- [ ] `docker-compose.yml` — Gitea + Jenkins (pinned image tags)
- [ ] `docker/jenkins/Jenkinsfile` — pipeline example
- [ ] Build: `docker build -t code-review-agent ./docker`
- [ ] Jenkins runs: `docker run --rm -e SCM_* -e LLM_* code-review-agent review`

### 3.2 Skip Mechanism
- [ ] Skip review if PR has label or title tag (e.g. `[skip-review]`)

### 3.3 Security
- [ ] Least-privilege token; repo-scoped only
- [ ] Container networking: restrict egress; allowlist SCM_URL

### Phase 3 Tests
- [ ] tests/cli/test_main.py — CLI parses args, invokes runner
- [ ] tests/docker/ — Dockerfile builds; docker-compose up
- [ ] CI smoke test: webhook triggers pipeline

---

## Phase 4: Comment Format and UX

### 4.1 Comment Structure
- [ ] Hidden marker: `<!-- code-review-agent:fingerprint=...;version=... -->`
- [ ] Body: [Critical]/[Suggestion]/[Info] prefix
- [ ] Location: path, line (or range)

### 4.2 PR Summary Comment
- [ ] PR-level summary: counts by severity; link to inline comments

### 4.3 Observability
- [ ] trace_id (UUID) per run
- [ ] Structured logs (JSON or key-value)
- [ ] Counters: PR size, files reviewed, tool calls, model latency, findings count, posts, resolves, retries
- [ ] Optional: Prometheus, OpenTelemetry export

### Phase 4 Tests
- [ ] tests/standards/test_prompts.py
- [ ] tests/formatters/test_comment_format.py
- [ ] tests/runner/test_observability.py

---

## Phase 5: Integration Testing

- [ ] E2E: Docker Compose up; seed Gitea; create PR; run agent; assert comments
- [ ] Golden tests: sample diffs + expected position mapping
- [ ] Rate limiting / retries: mocked 429 and transient failures
- [ ] Large PR fixture: validate chunking; no duplicate posts across file-by-file runs

---

## Phase 6: Provider Extensibility

### 6.1 GitHub Provider
- [ ] `providers/github.py` implements ProviderInterface
- [ ] GET /repos/.../pulls/{n} with Accept: application/vnd.github.v3.diff
- [ ] GET /repos/.../contents/{path}?ref={ref}
- [ ] GET /repos/.../pulls/{n}/files
- [ ] POST /repos/.../pulls/{n}/reviews (comments array)
- [ ] GET /repos/.../pulls/{n}/comments
- [ ] get_provider() supports "github"
- [ ] tests/providers/test_github.py (mocked HTTP)

### 6.2 GitLab Provider
- [ ] providers/gitlab.py
- [ ] tests/providers/test_gitlab.py

### 6.3 Bitbucket Provider
- [ ] providers/bitbucket.py
- [ ] tests/providers/test_bitbucket.py

### 6.4 Provider-Neutral Model
- [ ] Inline, file-level, PR-level comment types
- [ ] capabilities() per provider for suggested-change blocks (GitLab/GitHub)

---

## Key Files Summary

| File | Status |
|------|--------|
| agent/agent.py | Partial |
| agent/tools/gitea_tools.py | Partial (missing get_pr_diff_for_file, get_file_lines, detect_language_context) |
| agent/tools/review_helpers.py | Not started |
| config.py | Done (add github to SCM_PROVIDER) |
| models.py | Done |
| runner.py | Partial |
| providers/base.py | Partial (missing resolve_comment, get_pr_diff_for_file, get_file_lines, etc.) |
| providers/gitea.py | Done |
| providers/github.py | Not started |
| standards/detector.py | Done |
| standards/prompts/ | Done |
| schemas/findings.py | Done |
| diff/parser.py | Done |
| __main__.py | Partial (add --dry-run, --print-findings, --fail-on-critical) |
| docker/Dockerfile.agent | Not started |
| docker-compose.yml | Not started |
| docker/jenkins/Jenkinsfile | Not started |
