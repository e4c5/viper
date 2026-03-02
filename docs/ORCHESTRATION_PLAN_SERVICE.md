# Orchestration Service – High-Concurrency Review Plan (Sister Project)

This document outlines a design for a **separate orchestration service** that coordinates many `code-review-agent` runs across repositories and tenants.

The service is optional. CI can still invoke `code-review review` directly for low/medium concurrency.

---

## 1. Responsibilities (Out-of-Repo)

The orchestration service is **not** part of the `code-review-agent` package. It is a separate project that:

- Receives **webhooks** (or CI callbacks) on PR events.
- Enqueues **review jobs**.
- Enforces:
  - **Debounce**: “latest head_sha wins” per PR.
  - **Serialization**: at most one active review per PR/head at a time.
- Starts worker processes/containers that run:
  - `code-review review --owner ... --repo ... --pr ... --head-sha ...`

---

## 2. Core Data Model

### 2.1 Job Identity

For each review run, define:

- `provider` – e.g. `github`, `gitea`, `gitlab`, `bitbucket`
- `owner` – repo owner/org
- `repo` – repo name
- `pr_number` – PR/MR number
- `head_sha` – PR head commit

The **job id** mirrors the agent’s internal idempotency key:

```text
job_id = "{provider}/{owner}/{repo}/pr/{pr_number}/head/{head_sha}"
```

### 2.2 State Store (Recommended: Redis)

Use a small shared store (e.g. Redis) for:

- `latest_head:{provider}:{owner}:{repo}:{pr_number}` → latest known `head_sha`
- `lock:{provider}:{owner}:{repo}:{pr_number}` → current worker owner (with TTL)
- `job_state:{job_id}` → optional metadata (status, start/end timestamps, result).

---

## 3. Components

### 3.1 Webhook / Ingress API

- HTTP service (FastAPI/Flask/Go/etc.) that:
  - Accepts webhooks from SCM (GitHub/Gitea/GitLab/Bitbucket) or CI callbacks.
  - Extracts `provider`, `owner`, `repo`, `pr_number`, `head_sha` from payload.
  - Writes a compact job message to a queue (Redis stream/list, SQS, Pub/Sub, Kafka):
    - `{ provider, owner, repo, pr_number, head_sha }`.
  - Updates `latest_head:{...}` to the new `head_sha` (overwrite).
- Optionally enforces:
  - Signature verification / auth for webhooks.
  - Basic rate limiting per repo or tenant.

### 3.2 Worker(s)

One or more identical worker processes:

1. **Consume job** from the queue.
2. Look up `latest_head:{provider}:{owner}:{repo}:{pr_number}`:
   - If job’s `head_sha` != latest, **drop** the job (debounced).
3. Acquire **per-PR lock**:
   - `SETNX lock:{provider}:{owner}:{repo}:{pr_number} worker_id EX 300`
   - If the lock exists, either:
     - Requeue with delay, or
     - Drop (assuming a newer job will arrive).
4. **Launch review worker**:
   - Set env vars (`SCM_*`, `LLM_*`) from configuration/secret store.
   - Start a container or process running:
     - `code-review review --owner ... --repo ... --pr ... --head-sha ...`
5. Wait for completion:
   - Record job status in `job_state:{job_id}` (success, skip, error).
   - Release lock (`DEL lock:{...}`) or let TTL expire.

Workers can be scaled horizontally; locks ensure only one review per PR/head runs at a time.

---

## 4. Interaction with Agent Idempotency

The agent already:

- Computes an internal **idempotency key** that includes provider/owner/repo/pr/head_sha/agent_version/config_hash.
- Embeds it in a hidden comment marker.
- Skips runs for a given PR/head when it sees the same key in existing comments.

The orchestration service should:

- Treat this as **defence in depth**:
  - Even if a job is accidentally scheduled twice, the agent will not double-post.
- Optionally:
  - Read logs or metrics to know when a job was skipped due to idempotency.

No additional persistence is required inside the Python package for this.

---

## 5. Configuration & Tenancy

The orchestration service is where you model:

- **Tenants / organizations**:
  - Map webhook secrets, SCM tokens, and LLM config per tenant.
- **Routing**:
  - For each incoming webhook, determine:
    - Which tenant it belongs to.
    - Which `SCM_URL` and token to use.
    - Which LLM provider/model to use.
- **Concurrency limits**:
  - Global max concurrent runs.
  - Per-tenant or per-repo concurrency caps.

These aspects stay out of the `code-review-agent` repo and live entirely here.

---

## 6. Failure Handling

Recommended behaviours:

- **Worker crash or timeout**:
  - Rely on lock TTL; after expiry, future jobs can re-acquire the lock.
  - The queue can re-deliver or move the job to a dead-letter queue.
- **SCM / LLM transient failures**:
  - Let the inner agent process fail with non-zero exit.
  - The orchestration service decides:
    - Whether to retry the entire job (with backoff).
    - Or leave it as a failed run, visible in dashboards.

---

## 7. Deployment Patterns

A few concrete deployment options:

- **Kubernetes**:
  - Webhook service as Deployment + Service + Ingress.
  - Worker Deployment consuming from Redis/SQS/etc.
  - `code-review-agent` as a Docker image used by worker pods.
- **Jenkins-centric**:
  - Jenkins job triggers call into the webhook/ingress API.
  - Workers run as Jenkins agents (or inside Jenkins-managed Kubernetes pods).
- **GitHub Actions**:
  - Minimal ingress API; Actions workflow calls it with PR metadata.
  - Orchestration service then schedules containerized workers.

The exact choice depends on your existing infra; the **contract with this repo remains**:

- “Given SCM_* env and optional CLI args, run a single review.”

---

## 8. Documentation & Cross-Links

In this orchestration project’s README:

- Link back to:
  - `ORCHESTRATION_PLAN_AGENT.md` in the `code-review-agent` repo.
  - `QUICKSTART.md` / `DEVELOPER_GUIDE.md` for worker configuration.
- Explain:
  - How to configure SCM and LLM settings.
  - How to run the webhook service and workers.
  - How debouncing and locking work in practice.

This keeps the separation of concerns clear while giving operators a complete story for high-concurrency deployments.

