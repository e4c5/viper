# Development Testing Guide

Two short paths for testing during development: **Docker** and **non-Docker**.

---

## A) Docker (local Gitea + Jenkins)

1. Start the stack (repo root):
   ```bash
   docker compose up -d --build
   ```
   Podman users should set the socket path first:
   ```bash
   export CONTAINER_SOCKET=$XDG_RUNTIME_DIR/podman/podman.sock
   podman-compose up -d --build
   ```
2. Configure Gitea and Jenkins using **Quick Start** (includes the webhook setup):
   - See **[Quick Start Guide (Docker Only)](QUICKSTART.md)**.
3. Trigger a review:
   - Create/update a PR in Gitea → Jenkins auto-runs the review job.

### Optional: isolated E2E stack (separate volumes)

To run the E2E tests without touching your normal `gitea_data` / `jenkins_home` volumes, use the
separate Compose file and project name:

```bash
docker compose -f docker-compose.e2e.yml -p code-review-e2e up -d --build
RUN_E2E=1 pytest tests/e2e
docker compose -f docker-compose.e2e.yml -p code-review-e2e down -v
```

This uses different volume and container names (`gitea_data_e2e`, `jenkins_home_e2e`, etc.), so your
existing Docker data is not affected.

---

## B) Without Docker (run locally against any SCM)

1. Install:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Linux/macOS; on Windows: .venv\Scripts\activate
   pip install -e .
   ```
2. Set env vars (example for GitHub):
   ```bash
   export SCM_PROVIDER=github
   export SCM_URL=https://api.github.com
   export SCM_TOKEN=ghp_...
   export LLM_PROVIDER=gemini
   export GOOGLE_API_KEY=...
   ```
3. Run:
   ```bash
   code-review review --owner <owner> --repo <repo> --pr <pr_number> --head-sha <commit_sha>
   ```

---

## C) With an External Orchestration Service (Optional)

For high-concurrency testing, you can introduce a separate orchestration service (sister project) that:

- Receives SCM webhooks or CI callbacks.
- Enqueues review jobs and debounces by PR/head (latest `head_sha` wins).
- Starts worker processes/containers that run:
  - `code-review review --owner ... --repo ... --pr ... --head-sha ...`

In that setup:

- Use sections **A** or **B** to verify the worker image/CLI locally (one review at a time).
- Use the orchestration project’s own docs (see `ORCHESTRATION_PLAN_SERVICE.md`) to:
  - Run a local instance of the webhook/queue/worker stack.
  - Verify that multiple rapid PR updates result in a **single** review for the latest head SHA.

The Python package itself remains unchanged; only how you **schedule** reviews differs.
