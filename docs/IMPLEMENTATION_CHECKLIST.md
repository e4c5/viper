# Code Review Agent — Implementation Checklist

This checklist summarises concrete work items derived from `docs/IMPROVEMENT_PLAN.md`, validated
against `AGENTS.md` and `README.md`. It is intentionally concise; **always refer to
`IMPROVEMENT_PLAN.md` for full context, rationale, and detailed recommendations.**

Tick items as they are implemented.

---

## 1. Security

- [x] Implement SSRF protection and URL allow/deny-listing for `SCM_URL` (see `IMPROVEMENT_PLAN.md` §1.1).
- [x] Switch SCM tokens to `pydantic.SecretStr` (and prepare for LLM secrets) and update call sites accordingly (see §1.2).
- [x] Validate/sanitise `owner` and `repo` inputs at CLI/runner boundary (see §1.3).
- [x] Add HMAC signing or equivalent hardening for fingerprint markers (see §1.4).
- [x] Harden `get_file_content()` against malformed base64 responses in Gitea provider (see §1.5).
- [x] Move `pytest` and `pytest-asyncio` out of core dependencies into a dev extras group (see §1.6).

## 2. Code Quality and Architecture

- [x] Deduplicate shared provider logic into `ProviderInterface` (e.g. `get_pr_diff_for_file`, `get_file_lines`, `MAX_REPO_FILE_BYTES`) (see §2.1).
- [ ] Refactor `run_review()` into smaller helpers or a `ReviewOrchestrator`-style abstraction while preserving the documented flow in `AGENTS.md` (see §2.2).
- [x] Replace `@lru_cache` config caching with a resettable pattern suitable for tests (see §2.3).
- [x] Either wire `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` into the model client or clearly deprecate them in code and docs (see §2.4).
- [x] Decide on and implement a strategy for the legacy non–findings-only agent mode (remove or fully support) (see §2.5).
- [x] Fix language detection for `Makefile` so it is not a strong `cpp` signal (see §2.6).
- [x] Move the `Counter` import out of `_build_pr_summary_body()` to module scope (see §2.7).
- [x] Make the diff token budget ratio configurable or better tuned to large-context models (see §2.8).

## 3. Testing

- [x] Add a GitHub Actions (or equivalent) CI workflow that runs linting and tests on pushes/PRs (see §3.1).
- [x] Expand tests to cover error paths and failure modes described in the plan (see §3.2).
- [x] Move Gitea provider tests into `tests/providers/test_gitea.py` for layout consistency (see §3.3).
- [x] Turn the placeholder E2E test into a real smoke test of the full stack (see §3.4).
- [x] Add unit tests for `detect_from_paths_per_folder_root()` covering the edge cases listed (see §3.5).

## 4. CI/CD Pipeline

- [ ] Tighten `ruff` configuration and introduce mypy (or equivalent) type checking, then integrate both into CI (see §4.1).
- [ ] Pin the Docker base image by digest and add image scanning to CI (see §4.2).
- [ ] Introduce a `.pre-commit-config.yaml` with formatting, linting, and type-checking hooks (see §4.3).
- [ ] Improve Jenkins credential handling and documentation to decouple from hard-coded IDs (see §4.4).

## 5. Performance and Reliability

- [ ] Share and standardise HTTP retry logic across all SCM providers (see §5.1).
- [ ] Plan and, where appropriate, introduce limited parallelism or async HTTP calls for high-latency paths (see §5.2).
- [ ] Improve `_estimate_tokens()` to use model-appropriate tokenisers when available (see §5.3).
- [ ] Extend Prometheus histogram buckets and add error outcome metrics (see §5.4).
- [ ] Implement a real timeout around ADK `Runner.run()` using `LLM_TIMEOUT_SECONDS` (see §5.5).
- [ ] Clearly document the limitations of `InMemorySessionService` and, for orchestrated deployments, design a persistent session store (see §5.6).

## 6. Configuration and Dependency Management

- [ ] Move `litellm` into an optional dependency group and gate imports accordingly (see §6.1).
- [ ] Add strict URL validation for `SCM_URL` in `SCMConfig` (see §6.2).
- [ ] Clarify `SCM_SKIP_LABEL` behaviour in docs/env examples and consider additional flags/naming tweaks (see §6.3).

## 7. Developer Experience

- [ ] Add configurable structured logging (e.g. JSON format) and a `setup_logging()` entrypoint hook (see §7.1).
- [ ] Add a contributor guide (`CONTRIBUTING.md`) describing workflow, testing, linting, and extension patterns (see §7.2).
- [ ] Remove or relocate the internal planning file `docs/ai_code_review_agent_280caf7c.plan.md` (see §7.3).
- [ ] Improve tool docstrings in `create_findings_only_tools()` to match ADK expectations (see §7.4).

## 8. Feature Gaps

- [ ] Implement `resolve_comment()` for GitLab, including any schema changes needed for discussion IDs (see §8.1).
- [ ] Surface Bitbucket’s lack of label support via capabilities and user-facing warnings (see §8.2).
- [ ] Add a configurable minimum severity threshold for which findings are posted (see §8.3).
- [ ] Implement paginated fetching of existing review comments for GitHub and Gitea providers (see §8.4).
- [ ] Integrate monorepo-aware language detection into the runner using `detect_from_paths_per_folder_root()` (see §8.5).

## 9. Enterprise Readiness

These items are larger cross-cutting initiatives; see `IMPROVEMENT_PLAN.md` §9.x for detailed requirements and suggested roadmaps.

- [ ] Implement data-privacy features, including PII scrubbing, data residency controls, retention documentation, and provider no-training guarantees (see §9.1).
- [ ] Introduce a multi-tenant configuration model with strong tenant isolation for SCM/LLM config and session data (see §9.2).
- [ ] Design and add RBAC, admin APIs, and SSO/identity integration for enterprise deployments (see §9.3).
- [ ] Add structured, append-only audit logging for review runs, aligned with SOC 2 expectations (see §9.4).
- [ ] Integrate with a secrets manager and support secure secret rotation (see §9.5).
- [ ] Add health/readiness endpoints, circuit breakers, SLA timeouts, and DLQ handling for failed reviews (see §9.6).
- [ ] Implement per-tenant cost attribution, budgets, and LLM token usage metrics (see §9.7).
- [ ] Evolve the worker into a fully stateless component behind a work queue with per-PR locking, consistent with the orchestration plans (see §9.8 and `ORCHESTRATION_PLAN_SERVICE.md`).
- [ ] Strengthen prompt-injection defences, including message structuring, strict tool allowlists, and output validation (see §9.9).
- [ ] Define and execute a compliance certification roadmap (SOC 2, ISO 27001, GDPR/CCPA, etc.) as described in §9.10.
- [ ] Provide production-ready deployment artefacts for Kubernetes and air-gapped/self-hosted setups, and clarify feature tiers (see §9.11).
- [ ] Establish an OSS licence audit process and SBOM generation for releases (see §9.12).

