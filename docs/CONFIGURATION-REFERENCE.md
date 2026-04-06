# Configuration reference

Single place for **all environment variables** and related options used by the code review agent.

**Source of truth in code:** `src/code_review/config.py` (Pydantic `BaseSettings`), schemas under `src/code_review/schemas/`, plus a few ad-hoc reads in `runner.py`, `observability.py`, `logging_config.py`, `diff/fingerprint.py`, and `models.py`.

**Important:** The application does **not** load `.env` files automatically. Set variables in the process environment, CI secrets, or container env (or `source` a file yourself).

---

## Table of contents

1. [Quick reference](#1-quick-reference)
2. [SCM (`SCM_*`)](#2-scm-scm_)
3. [LLM (`LLM_*`)](#3-llm-llm_)
4. [Runner review behaviour](#4-runner-review-behaviour)
5. [Code review app (`CODE_REVIEW_*`)](#5-code-review-app-code_review_)
6. [Context-aware review (`CONTEXT_*`)](#6-context-aware-review-context_)
7. [Observability](#7-observability)
8. [Fingerprint markers](#8-fingerprint-markers)
9. [E2E UI / Jenkins automation (optional)](#9-e2e-ui--jenkins-automation-optional)
10. [Optional Python extras](#10-optional-python-extras)
11. [Further reading](#11-further-reading)

---

## 1. Quick reference

| Prefix / area | Purpose |
|---------------|---------|
| `SCM_*` | SCM provider, URL, token, PR coordinates, skip rules, review decision |
| `LLM_*` | Model provider, model name, API key, context/output limits |
| `LLM_DIFF_BUDGET_RATIO` | Fraction of context window used for diff before file-by-file mode |
| `CODE_REVIEW_*` | Logging, prompt toggles, raw-response debug, marker signing |
| `CONTEXT_*` | Optional linked-issue/Jira/Confluence enrichment; DB and embeddings |
| `CODE_REVIEW_METRICS` / `CODE_REVIEW_TRACING` | Prometheus / OpenTelemetry |
| `OTEL_*` | OTLP exporter endpoints (when tracing enabled) |

---

## 2. SCM (`SCM_*`)

Loaded via `SCMConfig` (`env_prefix="SCM_"`). Field names map to env vars in **UPPER_SNAKE_CASE** (e.g. `provider` → `SCM_PROVIDER`).

| Variable | Default | Description |
|----------|---------|-------------|
| `SCM_PROVIDER` | `gitea` | `gitea` \| `github` \| `gitlab` \| `bitbucket` \| `bitbucket_server` |
| `SCM_URL` | *(required)* | API base URL (HTTP/HTTPS only). |
| `SCM_TOKEN` | *(required)* | API token (`SecretStr`). |
| `SCM_OWNER` | `""` | Owner / workspace / project key (provider-specific). |
| `SCM_REPO` | `""` | Repository name or slug. |
| `SCM_PR_NUM` | — | PR/MR number (integer). |
| `SCM_HEAD_SHA` | `""` | Head commit SHA (needed to post comments). |
| `SCM_BASE_SHA` | `""` | Optional review base SHA. When set with `SCM_HEAD_SHA`, Viper reviews only the incremental `SCM_BASE_SHA..SCM_HEAD_SHA` changes; if unset, it reviews the full PR diff. |
| `SCM_EVENT` | `""` | Webhook event (e.g. `opened`). |
| `SCM_SKIP_LABEL` | `skip-review` | If PR has this label, skip review (empty disables). |
| `SCM_SKIP_TITLE_PATTERN` | `[skip-review]` | If title contains this substring, skip review (empty disables). |
| `SCM_REVIEW_DECISION_ENABLED` | `false` | Auto-submit PR review decision (provider-supported). |
| `SCM_REVIEW_DECISION_HIGH_THRESHOLD` | `1` | Request changes when open high-severity count ≥ this. |
| `SCM_REVIEW_DECISION_MEDIUM_THRESHOLD` | `3` | Request changes when open medium-severity count ≥ this. |
| `SCM_BITBUCKET_SERVER_USER_SLUG` | `""` | Bitbucket Server/DC only: username slug of the token user; required for `submit_review_decision` (participant API). When unset, `supports_review_decisions` is false for that provider. |
| `SCM_ALLOWED_HOSTS` | — | Optional comma-separated allowlist of SCM hosts; `SCM_URL` must match. |

**Review decisions vs merge blocking:** Only some providers implement automatic submission; whether `APPROVE` / `REQUEST_CHANGES` actually prevents merging depends on branch protection or merge checks on the SCM. See [SCM review decisions and merge blocking](SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md).

CLI flags (`--owner`, `--repo`, `--pr`, `--head-sha`) override or fill these when unset.

---

## 3. LLM (`LLM_*`)

Loaded via `LLMConfig` (`env_prefix="LLM_"`).

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `gemini` | `gemini` \| `openai` \| `anthropic` \| `ollama` \| `vertex` \| `openrouter` |
| `LLM_MODEL` | `gemini-2.5-flash` | Model identifier for the provider. |
| `LLM_API_KEY` | — | Single universal API key. Passed directly to the ADK `LiteLlm` constructor (no `os.environ` injection). |
| `LLM_CONTEXT_WINDOW` | `128000` | Context window in tokens (used for chunking / budgets). |
| `LLM_MAX_OUTPUT_TOKENS` | `4096` | Max output tokens for generation. |
| `LLM_TEMPERATURE` | `0.0` | Sampling temperature. |
| `LLM_DISABLE_TOOL_CALLS` | `false` | Debug: disable tool calls in the agent. |
| `LLM_TIMEOUT_SECONDS` | `60.0` | **Configuration-only** for now; not wired through ADK in all paths. |
| `LLM_MAX_RETRIES` | `3` | **Configuration-only** for now. |

**Ollama:** No API key required. `OLLAMA_API_BASE` (default `http://localhost:11434`) is the usual convention for LiteLLM/Ollama; see `docs/DEVELOPER_GUIDE.md`.

**Unified API Key handling:** `LLM_API_KEY` is passed directly to the `LiteLlm` constructor for all providers. Provider-specific properties like `GOOGLE_API_KEY` are **no longer supported or injected**.

---

## 4. Runner review behaviour

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_DIFF_BUDGET_RATIO` | `0.25` | Fraction of `LLM_CONTEXT_WINDOW` reserved for the unified diff; above this the runner switches to file-by-file review. |

---

## 5. Code review app (`CODE_REVIEW_*`)

| Variable | Default | Description |
|----------|---------|-------------|
| `CODE_REVIEW_LOG_LEVEL` | `WARNING` | `DEBUG`, `INFO`, `WARNING`, `ERROR` (case-insensitive). |
| `CODE_REVIEW_INCLUDE_COMMIT_MESSAGES_IN_PROMPT` | `true` | Include a PR commit-message block in the review prompt. |
| `CODE_REVIEW_REVIEW_DECISION_ONLY` | `false` | When `true` / `1`, skip the LLM and inline posting; only recompute the quality gate and submit a PR review decision (requires `SCM_REVIEW_DECISION_ENABLED` for submission). Same effect as CLI `--review-decision-only`. |
| `CODE_REVIEW_REVIEW_DECISION_ONLY_SKIP_IF_BOT_NOT_BLOCKING` | `false` | **Review-decision-only:** if `CODE_REVIEW_EVENT_COMMENT_ID` is set, skip the run when the SCM provider reports the token user is **not** in a blocking review state (`NOT_BLOCKING`). Empty event context always recomputes. Providers without `supports_bot_blocking_state_query` never skip on this path. |
| `CODE_REVIEW_REPLY_DISMISSAL_ENABLED` | `true` | **Review-decision-only:** when `CODE_REVIEW_EVENT_COMMENT_ID` is set, run the reply-dismissal flow on the review thread (GitHub, GitLab, Bitbucket Cloud, and Bitbucket Server / DC when `supports_review_thread_dismissal_context`). The runner may skip the LLM when the provider already indicates the concern is addressed (for example an applied/orphaned Bitbucket suggestion). Otherwise, if the model returns `agreed`, that thread is excluded from quality-gate counts for this run and, when the provider supports `supports_review_thread_resolution`, the thread is also resolved in the SCM. If `disagreed`, the runner posts a thread reply when the provider supports `supports_review_thread_reply` (unless `--dry-run`). Set to `false` to disable. **Gitea** does not implement thread context yet (`skipped_no_capability`). |
| `CODE_REVIEW_PRINT_RAW_RESPONSE` | *(unset)* | `1` / `true` / `TRUE` to log the raw LLM final response (debug). |
| `CODE_REVIEW_SIGNING_KEY` | *(unset)* | If set, HMAC-signs fingerprint markers in posted comments (see §8). |

### 5.1 Review-decision webhook context (`CODE_REVIEW_EVENT_*`)

Optional. When any of these is non-empty, the runner builds a `ReviewDecisionEventContext` (see `src/code_review/schemas/review_decision_event.py`) for **review-decision-only** runs: structured logging and bot-reply guard. Map from your webhook payload in CI (Generic Webhook Trigger, GitHub Actions `env`, etc.).

When `CODE_REVIEW_EVENT_ACTOR_LOGIN` or `CODE_REVIEW_EVENT_ACTOR_ID` identifies the same SCM user as the review token, review-decision-only runs short-circuit before recomputing the quality gate. This prevents Viper's own comment or reply activity from triggering a second bot pass.

| Variable | Description |
|----------|-------------|
| `CODE_REVIEW_EVENT_COMMENT_ID` | Comment id as string. |
| `CODE_REVIEW_EVENT_ACTOR_LOGIN` | Actor username / login. |
| `CODE_REVIEW_EVENT_ACTOR_ID` | Actor id as string. |

### 5.2 Jenkins bundled pipeline (`docker/jenkins/Jenkinsfile`)

The bundled Jenkinsfile automatically routes events based on `PR_ACTION`:

- **Comment/thread events** (`PR_ACTION` values like `pr:comment:added`, `issue_comment`, `pull_request_review_comment`, etc.) are routed to `code-review --review-decision-only`. `SCM_HEAD_SHA` may be omitted (resolved via SCM API).
- **PR lifecycle events** (`opened`, `synchronize`, `pr:opened`, `pr:from_ref_updated`, etc.) run the main review flow. When `SCM_BASE_SHA` is also provided, that flow is scoped to the incremental `base..head` range instead of the full PR diff.
- Optional Jenkins-only bot guard: set `CODE_REVIEW_BOT_USER_LOGIN` and/or `CODE_REVIEW_BOT_USER_ID` on the job or folder to skip bot-authored comment/thread webhook builds before Jenkins starts the agent. For Bitbucket Server / DC, `SCM_BITBUCKET_SERVER_USER_SLUG` is used as the login fallback automatically.

Set `SCM_REVIEW_DECISION_ENABLED=true` on the job so the quality-gate decision is submitted for both paths.

---

## 6. Context-aware review (`CONTEXT_*`)

Loaded via `ContextAwareReviewConfig` (case-insensitive env names). Optional feature: linked GitHub/GitLab issues, Jira, Confluence → cache in PostgreSQL → distill → `<context>` in prompt.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_AWARE_REVIEW_ENABLED` | `false` | Master switch. |
| `CONTEXT_AWARE_REVIEW_DB_URL` | — | PostgreSQL DSN when context-aware review is enabled (required). |
| `CONTEXT_GITHUB_ISSUES_ENABLED` | `false` | Fetch GitHub issue content for extracted refs. |
| `CONTEXT_GITLAB_ISSUES_ENABLED` | `false` | Fetch GitLab issue content for extracted refs. |
| `CONTEXT_JIRA_ENABLED` | `false` | Fetch Jira issues. |
| `CONTEXT_JIRA_URL` | `""` | Jira base URL. |
| `CONTEXT_JIRA_EMAIL` | `""` | Jira API user email. |
| `CONTEXT_JIRA_TOKEN` | — | Jira API token. |
| `CONTEXT_JIRA_EXTRA_FIELDS` | `""` | Comma-separated extra Jira field IDs/names. |
| `CONTEXT_CONFLUENCE_ENABLED` | `false` | Fetch Confluence pages. |
| `CONTEXT_CONFLUENCE_URL` | `""` | Confluence base URL. |
| `CONTEXT_CONFLUENCE_EMAIL` | `""` | Confluence API user email. |
| `CONTEXT_CONFLUENCE_TOKEN` | — | Confluence API token. |
| `CONTEXT_MAX_BYTES` | `20000` | Byte threshold: under = direct distillation; over = RAG path. |
| `CONTEXT_DISTILLED_MAX_TOKENS` | `4000` | Max output tokens for distilled context brief. |
| `CONTEXT_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model (RAG path; litellm). |
| `CONTEXT_EMBEDDING_DIMENSIONS` | `1536` | Vector dimensions for `pgvector` (must match model and schema). |
| `CONTEXT_GITHUB_API_URL` | `""` | Override GitHub API base when `SCM_PROVIDER` is not `github`. |
| `CONTEXT_GITHUB_TOKEN` | — | GitHub token when not using GitHub as SCM. |
| `CONTEXT_GITLAB_API_URL` | `""` | Override GitLab API base when `SCM_PROVIDER` is not `gitlab`. |
| `CONTEXT_GITLAB_TOKEN` | — | GitLab token when not using GitLab as SCM. |

**Dependencies:** `pip install -e ".[context]"` adds PostgreSQL client (`psycopg`) for the context store. Schema: `docs/PGVECTOR-SCHEMA.md`.

**Operator guide:** `docs/CONTEXT-AWARE-USER-GUIDE.md`  
**Internals:** `docs/CONTEXT-AWARE-DEVELOPER-GUIDE.md`

---

## 7. Observability

From `observability.py`. Requires `pip install -e ".[observability]"` for Prometheus/OTel libraries.

| Variable | Values | Description |
|----------|--------|-------------|
| `CODE_REVIEW_METRICS` | `prometheus` or truthy | Enable Prometheus metrics registry. |
| `CODE_REVIEW_PROMETHEUS` | `1` / `true` / `yes` | Alternative flag for Prometheus. |
| `CODE_REVIEW_TRACING` | `otel` or truthy | Enable OpenTelemetry tracing. |
| `CODE_REVIEW_OTEL` | `1` / `true` / `yes` | Alternative flag for OTel. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OTLP endpoint (traces). |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | — | OTLP traces endpoint (alternative). |

Run counter includes label `context_aware` (`true` / `false`) when Prometheus is enabled.

When Prometheus is enabled, **`code_review_reply_dismissal_total`** counts reply-dismissal paths in review-decision-only runs (label **`outcome`**: `agreed`, `disagreed`, `parse_failed`, `llm_error`, `skipped_no_capability`, `skipped_insufficient_thread`, `skipped_bot_author`, `skipped_scm_already_addressed`).

---

## 8. Fingerprint markers

| Variable | Description |
|----------|-------------|
| `CODE_REVIEW_SIGNING_KEY` | Optional secret used to HMAC-sign hidden marker payloads in comment bodies. If unset, markers may remain unsigned for backward compatibility. |

---

## 9. E2E UI / Jenkins automation (optional)

Used only by Playwright scripts under `e2e_ui/` (see `docs/E2E-UI-JENKINS.md`).

| Variable | Description |
|----------|-------------|
| `JENKINS_URL` | Jenkins base URL (default `http://localhost:8080` if unset in scripts). |
| `JENKINS_USERNAME` | Jenkins login user. |
| `JENKINS_PASSWORD` | Jenkins login password. |
| `E2E_UI_REPO_URL` | Git repo URL for “Pipeline script from SCM”. |
| `E2E_UI_BRANCH` | Branch for pipeline SCM (default `main`). |
| `E2E_UI_HEADED` | `1` for headed browser. |

`SCM_PROVIDER` / `SCM_URL` may be written by the single-SCM script into Jenkins global env.

---

## 10. Optional Python extras

| Extra | Purpose |
|-------|---------|
| `[dev]` | Tests, linting (e.g. `pytest`, `ruff`). |
| `[context]` | PostgreSQL + context-aware review (`psycopg`). |
| `[observability]` | Prometheus and OpenTelemetry clients. |

---

## 11. Further reading

- **README:** `README.md` — quick start and high-level behaviour.
- **SCM review decisions & merge blocking:** `docs/SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md` — per-host approve/needs-work semantics and branch settings.
- **Developer guide:** `docs/DEVELOPER_GUIDE.md` — architecture, SCM/LLM tables, logging, testing.
- **Context-aware:** `docs/CONTEXT-AWARE-USER-GUIDE.md`, `docs/CONTEXT-AWARE-DEVELOPER-GUIDE.md`, `docs/PGVECTOR-SCHEMA.md`.
- **Example env file:** `.env.example` (not auto-loaded; copy/export manually).
