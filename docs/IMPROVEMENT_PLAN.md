# Code Review Agent — Improvement Plan

This document is the output of a thorough code review of the `code-review-agent` project. It is
organized by concern area, from the most critical issues to quality-of-life improvements. Each
section explains _what_ the problem is, _where_ in the codebase it lives, and _what_ should be
done to fix it.

---

## Table of Contents

1. [Security](#1-security)
2. [Code Quality and Architecture](#2-code-quality-and-architecture)
3. [Testing](#3-testing)
4. [CI/CD Pipeline](#4-cicd-pipeline)
5. [Performance and Reliability](#5-performance-and-reliability)
6. [Configuration and Dependency Management](#6-configuration-and-dependency-management)
7. [Developer Experience](#7-developer-experience)
8. [Feature Gaps](#8-feature-gaps)

---

## 1. Security

### 1.1 No SSRF protection on `SCM_URL`

**Location:** `src/code_review/config.py` (`SCMConfig.url`), all providers.

Any value of `SCM_URL` is accepted and used verbatim as an HTTP endpoint. A compromised CI
environment that can write environment variables could redirect the agent to an internal metadata
service (e.g. `http://169.254.169.254/latest/meta-data/` on AWS/GCP).

**Recommendation:** Validate `SCM_URL` in `SCMConfig` using a Pydantic validator to ensure it is
an `http(s)` URL and that the host is not a known internal IP range or loopback address. Consider
adding an allowlist of trusted hosts (configurable via `SCM_ALLOWED_HOSTS`) for environments where
the SCM host is known in advance.

---

### 1.2 API token logged in idempotency key hash input (indirect exposure risk)

**Location:** `src/code_review/runner.py` — `_build_idempotency_key()`.

The idempotency key includes the SCM URL and LLM provider/model but does not include the token.
However, if logging is set to DEBUG, the entire `SCMConfig` object could be serialised and emit
the token. Pydantic's `SecretStr` type should be used for the `token` field to prevent accidental
exposure in logs, repr output, or serialised config dumps.

**Recommendation:**
- Change `SCMConfig.token` and any API key fields to `pydantic.SecretStr`.
- Update all provider constructors that receive `token: str` to accept and unwrap `SecretStr`.

---

### 1.3 No sanitisation of `owner` and `repo` path components

**Location:** All providers (`gitea.py`, `github.py`, `gitlab.py`, `bitbucket.py`).

`owner` and `repo` are concatenated directly into URL paths (e.g.
`f"/repos/{owner}/{repo}/pulls/{pr_number}.diff"`). Maliciously crafted values containing `/`,
`..`, or URL-encoded characters could traverse unintended API paths.

**Recommendation:** Validate `owner` and `repo` at the CLI boundary and in `run_review()` to
match `^[a-zA-Z0-9_.-]+$` (or use `urllib.parse.quote(…, safe="")` when constructing URL
segments). Reject requests where these values do not match the expected format.

---

### 1.4 Fingerprint marker can be forged via PR description

**Location:** `src/code_review/diff/fingerprint.py` — `parse_marker_from_comment_body()`.

The idempotency and deduplication system relies on a hidden HTML comment
(`<!-- code-review-agent:fingerprint=...;run=... -->`) inside comment bodies. An attacker with
write access to the PR (or who can post a comment) can forge this marker to trick the agent into
believing a review has already been posted, effectively preventing any review from being posted.

**Recommendation:** Consider signing the marker with an HMAC keyed on `SCM_TOKEN` (or a
separate `CODE_REVIEW_SIGNING_KEY`). Verify the HMAC when parsing markers and discard those that
fail verification. Alternatively, fetch idempotency state from an external, agent-controlled store
rather than trusting comment bodies.

---

### 1.5 `base64.b64decode` not wrapped against malformed API responses

**Location:** `src/code_review/providers/gitea.py` — `get_file_content()`.

If the Gitea API returns a `content` field that is not valid base64 (e.g. due to a server-side
encoding bug or a truncated JSON response), `base64.b64decode()` raises `binascii.Error` which is
not caught. This will propagate as an unhandled exception and abort the review run.

**Recommendation:** Wrap the base64 decode in a `try/except binascii.Error` and either raise a
clear `ValueError` with context or return an empty string with a warning log, matching the same
error-handling pattern used elsewhere in the providers.

---

### 1.6 `pytest` and `pytest-asyncio` listed as core runtime dependencies

**Location:** `pyproject.toml` — `[project.dependencies]`.

`pytest>=8.0` and `pytest-asyncio>=0.24` are listed in `dependencies` (installed in production
Docker image) rather than in `[project.optional-dependencies.dev]`. This ships testing
infrastructure into the production container, unnecessarily increasing the attack surface.

**Recommendation:** Move `pytest` and `pytest-asyncio` to the `[dev]` optional-dependency group.
Rebuild and verify the production image still works (`pip install -e .` without `[dev]`).

---

## 2. Code Quality and Architecture

### 2.1 Severe code duplication in providers

**Location:** `gitea.py`, `github.py`, `gitlab.py`, `bitbucket.py`.

The following methods are **copy-pasted identically** across all four providers:
- `get_pr_diff_for_file()` — full diff fetch + `parse_unified_diff` + slice by file path.
- `get_file_lines()` — `get_file_content()` + `splitlines()` + slice by start/end index.

Additionally, `MAX_REPO_FILE_BYTES = 16 * 1024` is redefined in each provider module.

**Recommendation:** Extract these into `ProviderInterface` as concrete (non-abstract) methods that
call `self.get_pr_diff()` and `self.get_file_content()` respectively. Providers that have a
faster native per-file diff endpoint can override the default. Move `MAX_REPO_FILE_BYTES` to
`providers/base.py` or `providers/safety.py` as a single module-level constant.

---

### 2.2 `run_review()` is a 300-line monolith

**Location:** `src/code_review/runner.py`.

The `run_review()` function handles every step of the orchestration:
configuration, provider setup, skip logic, existing-comment fetching, idempotency, language
detection, agent creation, session management, token budgeting, file-by-file iteration, response
parsing, fingerprinting, filtering, auto-resolve, comment posting (with fallback), PR summary,
and observability. This makes the function difficult to read, test in isolation, and extend.

**Recommendation:** Refactor into smaller, well-named helper functions or a
`ReviewOrchestrator` class with dedicated methods for each step (e.g.
`_should_skip_review()`, `_load_existing_state()`, `_run_agent()`, `_filter_findings()`,
`_post_findings()`). The existing private helper functions (`_build_ignore_set`,
`_fingerprint_for_finding`, etc.) are a good start; the inline logic in `run_review()` should be
similarly extracted.

---

### 2.3 `lru_cache` on config functions causes test pollution

**Location:** `src/code_review/config.py` — `get_scm_config()`, `get_llm_config()`.

These functions are decorated with `@lru_cache`, which means any test that sets environment
variables after module import will receive stale config. Tests currently work around this by
patching the functions entirely, but the coupling is fragile and can cause subtle failures when
test ordering changes.

**Recommendation:** Replace `@lru_cache` with a module-level singleton pattern that exposes a
`reset_config_cache()` helper (or use a context-manager fixture) that tests can call to flush the
cache between runs. This decouples tests from each other without requiring full mock patching for
simple configuration changes.

---

### 2.4 `LLM_TIMEOUT_SECONDS` and `LLM_MAX_RETRIES` are documented but never used

**Location:** `src/code_review/config.py` (`LLMConfig`), `docs/DEVELOPER_GUIDE.md`.

The developer guide explicitly acknowledges that these settings are "configuration-only for now."
However, they are advertised to users in `.env.example` and the README, creating false
expectations.

**Recommendation:** Either wire these into the ADK/LiteLLM client as soon as feasible (adding
an `httpx.Timeout` to the client or using LiteLLM's `request_timeout` parameter), or remove the
fields from `LLMConfig` and documentation until they are implemented. At minimum, add a prominent
warning in the docstring and `.env.example` noting the fields are currently inoperative.

---

### 2.5 Legacy `BASE_INSTRUCTION` / non-findings-only mode is effectively dead code

**Location:** `src/code_review/agent/agent.py`, `agent/tools/gitea_tools.py`.

`create_review_agent()` accepts `findings_only=False`, which activates the legacy
`BASE_INSTRUCTION` path giving the agent a `post_review_comment` tool. This mode has no tests,
no documentation for users, and is not reachable from the CLI. Maintaining it adds complexity and
a potential security surface (LLM-controlled comment posting).

**Recommendation:** Either remove the `findings_only=False` path entirely and clean up the dead
code in `gitea_tools.py` (`post_review_comment`, `get_existing_review_comments`), or promote it
to a documented, tested first-class feature with appropriate guardrails.

---

### 2.6 Makefile erroneously mapped to `cpp` in language detector

**Location:** `src/code_review/standards/detector.py` — `_PATH_SIGNALS`.

`Makefile` is mapped to `("cpp", None)` in `_PATH_SIGNALS`. While Makefiles are common in C/C++
projects, they are used equally in Python, Go, Ruby, and other projects. This causes the detector
to confidently announce `cpp` for any project that happens to include a Makefile, even if all
other signals point to a different language.

**Recommendation:** Remove `Makefile` from `_PATH_SIGNALS` (it is too ambiguous to be a language
signal) or treat it as a weak signal with a low weight that only tips the balance when other
signals are tied.

---

### 2.7 `_build_pr_summary_body()` imports `Counter` inside the function

**Location:** `src/code_review/runner.py` — `_build_pr_summary_body()`.

`from collections import Counter` is placed inside the function body. This is a minor style
inconsistency that mixes module-level and deferred imports.

**Recommendation:** Move the `Counter` import to the top of the file with the other standard
library imports.

---

### 2.8 `DIFF_TOKEN_BUDGET_RATIO = 0.25` may be too conservative

**Location:** `src/code_review/runner.py`.

The diff is allocated only 25 % of the context window. For a 128 k-token window the diff budget
is 32 k tokens (~128 KB of diff text). For models with larger context windows (e.g. 1 M tokens)
this wastes enormous capacity and causes unnecessary file-by-file splitting.

**Recommendation:** Either increase the ratio (e.g. to 0.5 or higher) or make it configurable
via an `LLM_DIFF_BUDGET_RATIO` environment variable, allowing operators to tune it per-model.

---

## 3. Testing

### 3.1 No CI workflow defined in the repository

**Location:** `.github/` (absent), repository root.

There is no GitHub Actions (or any other CI) workflow file. This means the test suite is never
run automatically on pull requests, and regressions are only caught if a developer manually runs
`pytest` locally.

**Recommendation:** Add a `.github/workflows/ci.yml` that runs on every push and pull request:
```
- Checkout code
- Set up Python (matrix: 3.10, 3.11, 3.12)
- pip install -e ".[dev]"
- ruff check src tests
- pytest --ignore=tests/e2e --cov=src/code_review --cov-report=xml
```
Optionally upload coverage to Codecov or a similar service.

---

### 3.2 No test coverage for most error paths

**Location:** `tests/` (general).

The current tests cover the happy path well (agent runs, findings posted, duplicates filtered).
The following error conditions have no coverage:

- Provider HTTP errors (4xx/5xx from SCM APIs) during `get_pr_diff`, `get_pr_files`,
  `get_existing_review_comments`, etc.
- `post_review_comments` fails for all comments (the batch-then-one-by-one fallback).
- The per-comment fallback itself fails and `post_pr_summary_comment` is called.
- `get_file_content` raises during fingerprint computation — runner should log and continue.
- Malformed JSON in agent response (only partial JSON, truncated, non-JSON).
- `detect_from_paths` returning `"unknown"` language — runner should not crash.

**Recommendation:** Add parametrised tests for each of these error branches. Use `respx` (already
a dev dependency) for provider HTTP error simulation and pytest's `pytest.raises` / `caplog`
for exception and log assertions.

---

### 3.3 `test_providers_gitea.py` duplicates provider test patterns

**Location:** `tests/test_providers_gitea.py` (root-level), `tests/providers/test_github.py`, etc.

Gitea provider tests live at the root of `tests/` while all other provider tests are in
`tests/providers/`. This is inconsistent and makes it harder to run provider tests as a group
(`pytest tests/providers/`).

**Recommendation:** Move `tests/test_providers_gitea.py` to `tests/providers/test_gitea.py` and
update any imports or pytest configurations accordingly.

---

### 3.4 E2E tests are placeholder-only

**Location:** `tests/e2e/test_docker_gitea_e2e.py`.

The E2E test directory exists but contains placeholder tests that do not exercise real
functionality. Running `RUN_E2E=1` still produces no meaningful assertions.

**Recommendation:** Implement at minimum one real E2E test that:
1. Stands up Gitea via `docker compose`.
2. Creates a repo and opens a PR.
3. Runs the agent CLI with `--dry-run`.
4. Asserts that findings are parsed and returned.

This provides a smoke test for the full integration stack without requiring a real LLM (use a
`MockProvider`-equivalent or the `LLM_DISABLE_TOOL_CALLS=true` mode).

---

### 3.5 Missing tests for the `detect_from_paths_per_folder_root` monorepo function

**Location:** `src/code_review/standards/detector.py` — `detect_from_paths_per_folder_root()`.

This function supports monorepo detection (multiple package roots) but has no dedicated tests.
Given the complexity of the grouping logic (longest-prefix matching, orphan handling), it is a
likely source of subtle bugs.

**Recommendation:** Add unit tests covering: single root, two roots, orphan files, overlapping
paths, empty input, and paths that are exactly equal to a root.

---

## 4. CI/CD Pipeline

### 4.1 No linting or type checking in the development workflow

**Location:** `pyproject.toml` — `[tool.ruff]`.

The `ruff` configuration exists but only sets `line-length` and `target-version`. No select/ignore
rules are configured, and there is no mypy or pyright configuration. Running `ruff check src`
with only defaults leaves many common issues unchecked.

**Recommendation:**
- Configure `ruff` with a meaningful rule set (e.g. `select = ["E", "F", "I", "B", "C4",
  "UP"]`) in `pyproject.toml`.
- Add a `[tool.mypy]` section with `strict = true` (or at minimum `disallow_untyped_defs =
  true`) and fix existing type errors.
- Add linting and type checking as steps in the CI workflow (see §4.1 above).

---

### 4.2 Docker image does not pin base image digest

**Location:** `docker/Dockerfile.agent`.

`FROM python:3.12-slim` uses a floating tag. A base-image update could silently introduce a
vulnerability or break the build.

**Recommendation:** Pin the base image to a specific digest (e.g.
`FROM python:3.12-slim@sha256:<digest>`) or use a tool like Dependabot or Renovate to
automatically propose base-image upgrades as pull requests. Add the Dockerfile to your CI
pipeline's security scanning step (e.g. using Trivy or Docker Scout).

---

### 4.3 No pre-commit hooks

**Location:** Repository root (`.pre-commit-config.yaml` absent).

Without pre-commit hooks, code style and lint violations can be committed and only caught during
code review or CI.

**Recommendation:** Add a `.pre-commit-config.yaml` with hooks for `ruff`, `ruff-format` (or
`black`), and `mypy`. This ensures consistent style without requiring developers to remember to
run linters manually.

---

### 4.4 Jenkinsfile couples credentials to specific IDs

**Location:** `docker/jenkins/Jenkinsfile`.

The Jenkinsfile references credential IDs (`SCM_TOKEN`, `GOOGLE_API_KEY`, etc.) by hard-coded
strings. If operators use different credential naming conventions or a different secrets manager,
the pipeline breaks silently.

**Recommendation:** Document the required credential IDs prominently at the top of the
Jenkinsfile as comments, and consider making them configurable via Jenkins parameters or
environment variables at the pipeline level.

---

## 5. Performance and Reliability

### 5.1 Retry logic is inconsistent across providers

**Location:** `src/code_review/providers/gitea.py` — `_request_with_retry()`.

Only `GiteaProvider` implements retry logic for `429 / 5xx` responses. `GitHubProvider`,
`GitLabProvider`, and `BitbucketProvider` use `httpx.Client` directly without any retry
mechanism. All four providers share the same vulnerability to transient network errors.

**Recommendation:** Extract the retry logic into a shared utility (e.g.
`providers/http.py::request_with_retry()`) and use it in all providers. The Gitea implementation
uses a fixed `time.sleep(1.0)` delay; replace with exponential back-off with jitter:
`min(RETRY_DELAY * 2 ** attempt + random.uniform(0, 0.5), MAX_DELAY)`.

---

### 5.2 All HTTP calls are synchronous

**Location:** All providers, `runner.py`.

The runner performs multiple sequential HTTP requests (get PR info, get existing comments, get PR
files, get diff, get file content, post comments). These are all synchronous `httpx.Client` calls.
For large PRs with many files, the file-by-file review loop issues one HTTP request per file
sequentially.

**Recommendation:** In a future async refactor, consider using `httpx.AsyncClient` with
`asyncio.gather()` to parallelise independent fetches (e.g. fetching file content at head SHA
for all changed files simultaneously). As an incremental step, the `get_file_lines_by_path`
helper in the runner could be parallelised using `concurrent.futures.ThreadPoolExecutor`.

---

### 5.3 `_estimate_tokens()` heuristic is inaccurate

**Location:** `src/code_review/runner.py` — `_estimate_tokens()`.

Token estimation uses `len(text) // 4`. This underestimates token counts for code with long
identifiers, Unicode characters, or dense whitespace and overestimates for many CJK or other
multi-byte characters. The result is that the file-by-file split threshold can be wrong,
either splitting unnecessarily or sending diffs that exceed the context window.

**Recommendation:** Use a proper tokeniser (e.g. `tiktoken` for OpenAI/Anthropic models, or
the model's own tokenisation API) when available. Fall back to the char/4 heuristic only when
no tokeniser is available. The function should be injectable / mockable for testing.

---

### 5.4 Prometheus histogram buckets do not cover slow runs

**Location:** `src/code_review/observability.py` — `_init_prometheus()`.

The histogram buckets `(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)` cap at 60 seconds. LLM
calls routinely take 30–120 seconds, and file-by-file reviews of large PRs can run for several
minutes. All runs beyond 60 seconds are bucketed together, providing no useful latency
percentile data for slow reviews.

**Recommendation:** Add higher buckets: `(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0,
300.0)`. Also add an `error` outcome to `_prometheus_run_counter` so operators can alert on
failed runs distinct from skipped runs.

---

### 5.5 No timeout on ADK `Runner.run()` call

**Location:** `src/code_review/runner.py` — `_run_agent_and_collect_response()`.

The `LLM_TIMEOUT_SECONDS` configuration field exists but is not wired to any actual timeout.
If an LLM API call hangs (e.g. due to a network partition), the runner will block indefinitely,
holding up CI pipelines.

**Recommendation:** Wrap `_run_agent_and_collect_response()` with a timeout using
`concurrent.futures` or Python's `asyncio.wait_for`. Raise a clear error (and return `[]`)
when the timeout is exceeded. Log the timeout event and record it in observability metrics.

---

### 5.6 `InMemorySessionService` does not survive process restarts

**Location:** `src/code_review/runner.py`.

ADK sessions are stored in memory for the lifetime of the process. If the review agent is
interrupted (container OOM kill, signal, crash), all in-flight review state is lost and the run
must start from scratch. For one-shot containers this is tolerable, but for the orchestrated
multi-tenant scenario described in `ORCHESTRATION_PLAN_AGENT.md`, this means no progress
resumption is possible.

**Recommendation:** Document this limitation clearly. For the orchestrated scenario, consider
using a persistent session service (e.g. backed by Redis or a database) so interrupted reviews
can be resumed without re-running from the beginning.

---

## 6. Configuration and Dependency Management

### 6.1 `litellm` is a heavy core dependency

**Location:** `pyproject.toml` — `dependencies`.

`litellm>=1.50` is a very large package (hundreds of transitive dependencies) and is only used
when `LLM_PROVIDER` is `openai`, `anthropic`, or `ollama`. Users who only use Gemini or Vertex
(the documented defaults) still install the full LiteLLM dependency graph.

**Recommendation:** Move `litellm` to an optional dependency group (e.g.
`[project.optional-dependencies.litellm]`) and gate its import behind a lazy check in
`models.py`. Users who only need Gemini can install with `pip install -e .` while LiteLLM
users install with `pip install -e ".[litellm]"`.

---

### 6.2 No validation that `SCM_URL` is a reachable URL

**Location:** `src/code_review/config.py` — `SCMConfig`.

Pydantic accepts any string for `url`. If an operator sets `SCM_URL` to an invalid value (e.g.
accidentally omitting the scheme), the error surfaces as a confusing `httpx.InvalidURL` deep
inside a provider method rather than at startup.

**Recommendation:** Add a `@field_validator("url")` in `SCMConfig` that checks the value is a
valid `http(s)` URL (using `urllib.parse.urlparse` and asserting `scheme in ("http", "https")`
and `netloc` is non-empty). This provides a clear, early error message.

---

### 6.3 `SCM_SKIP_LABEL` defaults to `"skip-review"` even when empty strings are intended to disable

**Location:** `src/code_review/config.py` — `SCMConfig.skip_label`.

The current code uses a truthiness check (`if cfg.skip_label and cfg.skip_label.strip()`),
which correctly treats `""` as disabled. However, the default value `"skip-review"` is
non-empty, so skip-label checking is always active unless the operator explicitly sets the env
var to `""`. This surprises users who have labels with similar names and wonder why reviews are
being skipped.

**Recommendation:** Keep the default, but add a note in `.env.example` and the documentation
that setting `SCM_SKIP_LABEL=` (empty) disables the feature. Consider renaming the field to
`skip_label_name` for clarity, or exposing a separate boolean `SCM_SKIP_LABEL_ENABLED` flag.

---

## 7. Developer Experience

### 7.1 No structured logging format guidance

**Location:** `src/code_review/runner.py` — `_log_run_complete()` and all `logger.*` calls.

The project uses `logger.info("run_complete", extra={...})` for the structured run-complete log
entry. However, there is no log format configured in the package, so by default Python's
`logging` will render this as a plain text string that ignores the `extra` dict. Operators who
want structured (JSON) logs for their log aggregation systems (e.g. Datadog, Loki, Splunk) must
configure the log format themselves, but are not told how.

**Recommendation:** Add a `CODE_REVIEW_LOG_FORMAT` environment variable that, when set to
`"json"`, configures a `python-json-logger` (or a simple custom `Formatter`) that serialises
all log records (including `extra` fields) as JSON. Document this in the README under
Observability. Provide a `setup_logging()` function that the CLI entrypoint calls at startup.

---

### 7.2 No contribution guide

**Location:** `docs/` (absent).

There is a `DEVELOPER_GUIDE.md` but no `CONTRIBUTING.md`. New contributors must infer the
development workflow, testing expectations, and PR requirements from context.

**Recommendation:** Add a `CONTRIBUTING.md` (or a `docs/CONTRIBUTING.md`) covering:
- How to set up the development environment (`pip install -e ".[dev]"`).
- How to run tests (`pytest --ignore=tests/e2e`).
- How to run the linter and type checker.
- The branch and PR strategy.
- How to add a new SCM provider or LLM backend.

---

### 7.3 `docs/ai_code_review_agent_280caf7c.plan.md` should not be committed

**Location:** `docs/ai_code_review_agent_280caf7c.plan.md`.

This file appears to be an internal planning artifact with a randomly generated ID in its name.
It is not referenced from any other documentation and adds noise to the `docs/` directory for
external contributors.

**Recommendation:** Remove this file (or move it to a separate internal planning repository) to
keep the `docs/` directory clean and focused on user-facing documentation.

---

### 7.4 Tool docstrings missing from `create_findings_only_tools`

**Location:** `src/code_review/agent/tools/gitea_tools.py` — `create_findings_only_tools()`.

The tools in `create_findings_only_tools` (used in production findings-only mode) lack the
detailed Google-style docstrings that `create_gitea_tools` has. ADK uses these docstrings to
describe tools to the LLM, so missing or minimal docstrings degrade the quality of tool
selection by the agent.

**Recommendation:** Add full docstrings (with `Args:` and `Returns:` sections) to all functions
in `create_findings_only_tools`, matching the style used in `create_gitea_tools`.

---

## 8. Feature Gaps

### 8.1 GitLab `resolve_comment` is a silent no-op

**Location:** `src/code_review/providers/gitlab.py` — `resolve_comment()`.

GitLab MR discussions can be resolved via the API
(`PUT /projects/:id/merge_requests/:mr_iid/discussions/:discussion_id`). The current
implementation has an empty `pass` body with a comment saying "not implemented." The
`capabilities()` method returns `resolvable_comments=False` to avoid calling it, but this means
auto-resolve of stale comments never works on GitLab even though the API supports it.

**Recommendation:** Implement `resolve_comment` for GitLab. The `comment_id` stored in
`ReviewComment.id` is a note ID; to resolve the discussion, the provider must also store the
`discussion_id`. Consider changing `ReviewComment` to include an optional `discussion_id` field
and returning the discussion ID from `get_existing_review_comments` for GitLab.

---

### 8.2 Bitbucket Cloud does not support labels — skip-by-label silently does nothing

**Location:** `src/code_review/providers/bitbucket.py` — `get_pr_info()`.

The code includes a comment acknowledging that Bitbucket Cloud does not support PR labels. When
`SCM_SKIP_LABEL` is configured and the provider is Bitbucket, the skip-by-label check fetches
PR info but always finds zero labels and never skips, giving users no feedback.

**Recommendation:** Log a `WARNING` once at startup (or on first skip-check for Bitbucket)
noting that label-based skip is not supported by this provider. Consider adding a
`capabilities().supports_pr_labels` flag to `ProviderCapabilities` and checking it in the
runner before attempting the label check.

---

### 8.3 No support for review severity thresholds / filtering before posting

**Location:** `src/code_review/runner.py`.

All findings (critical, suggestion, info) are posted to the PR by default. In busy projects this
can create noise. The `--fail-on-critical` CLI flag exists, but there is no way to post _only_
findings above a minimum severity (e.g. post only critical and suggestion, suppress info).

**Recommendation:** Add a `SCM_MIN_SEVERITY` environment variable (or `--min-severity` CLI
option) with valid values `critical | suggestion | info` (default `info` = post all). Filter
findings in `run_review()` before building `to_post`, and document the option in the README.

---

### 8.4 No paginated comment fetching

**Location:** `src/code_review/providers/github.py` — `get_existing_review_comments()`,
`src/code_review/providers/gitea.py` — `get_existing_review_comments()`.

GitHub and Gitea providers fetch existing review comments with `per_page=100` and `page=1`
respectively but do not paginate. A PR with more than 100 existing comments (common in
long-running PRs) will silently miss comments from subsequent pages, breaking deduplication.

**Recommendation:** Implement pagination for `get_existing_review_comments` in the GitHub and
Gitea providers (the Bitbucket provider already does this). For GitHub, follow the `Link` header
for pagination; for Gitea, increment the `page` parameter until an empty result is returned.

---

### 8.5 No monorepo-aware review (multi-language PRs)

**Location:** `src/code_review/runner.py`, `src/code_review/standards/detector.py`.

`detect_from_paths_per_folder_root()` exists in `detector.py` and is designed for monorepo
detection, but the runner calls the simpler `detect_from_paths()` which returns a single
language for the entire PR. In a monorepo PR that touches both a Python backend and a TypeScript
frontend, the agent receives review standards for only one language.

**Recommendation:** Call `detect_from_paths_per_folder_root()` in the runner when multiple roots
are detected. For each root, generate the appropriate review standards string and pass them all
to the agent (or, in file-by-file mode, use the per-file root's standards for that file's
review session). Update the agent instruction to reflect the multi-language context.

---

## Summary Table

| # | Area | Severity | Effort |
|---|------|----------|--------|
| 1.1 | SSRF protection on `SCM_URL` | High | Medium |
| 1.2 | `SecretStr` for tokens | Medium | Low |
| 1.3 | `owner`/`repo` path sanitisation | High | Low |
| 1.4 | Forgeable idempotency markers | Medium | High |
| 1.5 | `base64.b64decode` error handling | Low | Low |
| 1.6 | `pytest` in core deps | Medium | Low |
| 2.1 | Duplicated provider methods | Medium | Medium |
| 2.2 | `run_review()` monolith | Medium | High |
| 2.3 | `lru_cache` test pollution | Low | Low |
| 2.4 | Fake config fields | Low | Low |
| 2.5 | Dead legacy agent mode | Low | Low |
| 2.6 | `Makefile` → `cpp` mapping | Low | Low |
| 2.7 | Inline `Counter` import | Low | Low |
| 2.8 | Conservative diff token budget | Low | Low |
| 3.1 | No CI workflow | High | Low |
| 3.2 | Missing error-path tests | High | Medium |
| 3.3 | Inconsistent test layout | Low | Low |
| 3.4 | Placeholder E2E tests | Medium | High |
| 3.5 | Missing monorepo detector tests | Medium | Low |
| 4.1 | No linting rules / type checking | Medium | Low |
| 4.2 | Unpinned Docker base image | Medium | Low |
| 4.3 | No pre-commit hooks | Low | Low |
| 4.4 | Hard-coded Jenkins credential IDs | Low | Low |
| 5.1 | Inconsistent retry logic | High | Medium |
| 5.2 | Synchronous HTTP calls | Low | High |
| 5.3 | Inaccurate token estimation | Medium | Medium |
| 5.4 | Prometheus histogram range | Low | Low |
| 5.5 | No ADK run timeout | High | Medium |
| 5.6 | In-memory session limitation | Low | High |
| 6.1 | `litellm` as core dependency | Medium | Low |
| 6.2 | No `SCM_URL` format validation | Low | Low |
| 6.3 | Confusing skip-label default | Low | Low |
| 7.1 | No structured log format guide | Low | Low |
| 7.2 | No CONTRIBUTING.md | Low | Low |
| 7.3 | Internal planning doc committed | Low | Low |
| 7.4 | Missing tool docstrings | Medium | Low |
| 8.1 | GitLab resolve_comment not impl. | Medium | Medium |
| 8.2 | Bitbucket label skip silent | Low | Low |
| 8.3 | No severity threshold filtering | Medium | Low |
| 8.4 | No paginated comment fetching | High | Medium |
| 8.5 | No monorepo-aware review | Low | High |
