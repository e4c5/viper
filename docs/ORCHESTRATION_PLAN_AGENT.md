# Code Review Agent – Orchestration Integration Plan (Core Package)

This document describes how the existing **code-review-agent** package should evolve to work cleanly with an optional, external **orchestration service** that handles high-concurrency, debouncing, and queueing of review runs.

The focus here is **what changes belong in this repository only**.

---

## Goals

- Keep this package as a **stateless worker** that:
  - Accepts PR metadata (provider, owner, repo, pr_number, head_sha, base_sha) via CLI args/env.
  - Runs **one** review for that PR/head.
  - Uses internal idempotency/fingerprinting to avoid duplicate comments.
- Make it easy for:
  - CI systems to call the agent directly (no orchestrator).
  - A separate orchestration project to drive many concurrent reviews across PRs/tenants.

---

## 1. Public Contract for Orchestration

### 1.1 CLI & Env Contract (Current State)

The CLI already exposes a stable entry point:

- Command: `code-review`
- Inputs via args or env:
  - `SCM_PROVIDER`, `SCM_URL`, `SCM_TOKEN`
  - `SCM_OWNER`, `SCM_REPO`, `SCM_PR_NUM`
  - `SCM_HEAD_SHA`, `SCM_BASE_SHA` (optional but required for posting)
  - LLM config: `LLM_PROVIDER`, `LLM_MODEL`, etc.

**Plan:** Keep this contract as the **only required integration surface** for the orchestrator. No extra APIs are strictly necessary.

### 1.2 Idempotency & Run Identity (Current State)

`runner._build_idempotency_key` already computes a stable key:

- Key shape:\
  `{provider}/{owner}/{repo}/pr/{pr_number}/head/{head_sha}/agent/{AGENT_VERSION}/config/{config_hash}`

`run_review` embeds this as `run=<key>` in comment markers and skips runs that have already processed this key.

**Plan:**

- Document this key in the developer docs as the canonical **“review job ID”**.
- Do **not** expose a separate persistent store here; the orchestrator can:
  - Treat this key as an opaque identifier.
  - Rely on runner behaviour (comment markers) plus its own store for higher-level coordination.

---

## 2. Observability & Diagnostics for Orchestration

The orchestrator will need to monitor runs across many workers.

### 2.1 Structured Logs (Current State)

`runner._log_run_complete` emits a final `run_complete` log event with:

- `trace_id`
- `owner`, `repo`, `pr_number`
- `files_count`, `findings_count`, `posts_count`
- `duration_ms`

**Plan:**

- Keep this format stable and documented in `DEVELOPER_GUIDE.md`.
- Encourage orchestration service to:
  - Inject a **correlation id** (e.g. `JOB_ID` env var) which can be propagated into logs later if needed.
  - Aggregate these logs in whatever log system it uses (e.g. Loki/ELK).

### 2.2 Prometheus / OTel (Current State)

`code_review.observability` already supports optional Prometheus and Otel.

**Plan:**

- No code changes strictly required.
- Document in `DEV_TESTING.md` / `DEVELOPER_GUIDE.md` that:
  - When running in a container, the orchestrator can:
    - Scrape Prometheus metrics from the worker container (if exposed), or
    - Consume Otel traces exported by the worker process.

---

## 3. Behaviour Required for Safe Orchestration

The orchestration layer will handle queueing and debouncing, but this package must preserve a few guarantees.

### 3.1 Single-Run Semantics

**Invariant:** One invocation of `run_review` should:

- Make **at most one pass** over the PR/head.
- Respect its own idempotency key and comment markers.

**Plan:** Keep `run_review` as a single-shot orchestrator:

- No internal background threads.
- No retries that outlive the process.

### 3.2 Skip/Fail Signalling

Orchestration must be able to distinguish:

- “Skipped” (e.g. `[skip-review]` or idempotency key already present).
- “Successful” (ran, may or may not post comments).
- “Errored” (non-zero exit, exception).

**Plan:**

- Keep skip conditions **fast** and side-effect-free:
  - Skip label/title: return `[]`.
  - Idempotency key already seen: return `[]`.
- Ensure we **continue to return a non-zero exit code** on genuine failures (this is already the default behaviour if unhandled exceptions escape).
- Optionally (future), document recommended exit-code semantics in `DEVELOPER_GUIDE.md` for orchestration consumption.

---

## 4. No Orchestrator: Direct CI Mode

This package must remain usable without the external service.

**Plan:**

- Keep `QUICKSTART.md` and existing Jenkins example unchanged:
  - CI can call `code-review` directly with `SCM_*` env vars.
- Clearly document in a new section (e.g. “High-Concurrency / Orchestrated Mode”) that:
  - For low/medium concurrency, direct CI invocation is fine.
  - For high concurrency / many repos, use the **sister orchestration project**.

---

## 5. Documentation Changes in This Repo

To support the orchestration project cleanly:

- **`DEVELOPER_GUIDE.md`**
  - Add a short **“Orchestrated Deployment”** subsection:
    - Describe the idempotency key shape.
    - Describe expectations (single-shot run, skip behaviour, exit codes).
    - Point to the sister project for queue/debounce.

- **`IMPLEMENTATION_TASKS.md`**
  - For Optional Task F.1, reference:
    - This document (agent-side plan).
    - The orchestration-side plan (`ORCHESTRATION_PLAN_SERVICE.md`).

No additional Python modules are strictly required in this repo to support the orchestration layer; the current entrypoints and idempotency behaviour are sufficient.

