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

### Optional: isolated E2E stack (separate volumes, auto-managed by pytest)

To run E2E tests without touching your normal `gitea_data` / `jenkins_home` volumes, this repo
provides an isolated Compose stack under `tests/e2e/docker-compose.e2e.yml` plus a pytest fixture
that starts and stops it automatically.

#### One-command E2E (hello-world PR auto-created)

The E2E test will automatically:

- Start Gitea + Jenkins via the isolated Compose stack.
- Use the Gitea API to create (or reuse) a small `code-review-e2e-hello` repo.
- Create a feature branch and a “hello world” PR.
- Run the agent against that PR with a stubbed LLM.

From the repo root:

```bash
RUN_E2E=1 pytest -m e2e
```

No manual `GITEA_E2E_TOKEN` export is required; the E2E setup code handles creating and using a
Gitea access token internally.

What happens:

- `tests/conftest.py::e2e_stack` uses `docker compose -f tests/e2e/docker-compose.e2e.yml -p code-review-e2e up -d`
  to start Gitea + Jenkins once per test session.
- `tests/e2e/test_docker_gitea_e2e.py` calls the Gitea REST API using an internally created access
  token to create the hello-world repo and PR and then runs the agent via `run_review(...)`
  (with LLM stubbed).
- When the session finishes, pytest tears the stack down with
  `docker compose -f tests/e2e/docker-compose.e2e.yml -p code-review-e2e down -v`.

If you change Dockerfiles and need to rebuild images, set `E2E_REBUILD=1`:

```bash
E2E_REBUILD=1 RUN_E2E=1 pytest -m e2e
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
   export LLM_API_KEY=...
   ```
3. Run:
   ```bash
   code-review --owner <owner> --repo <repo> --pr <pr_number> --head-sha <commit_sha>
   ```

---

## C) With an External Orchestration Service (Optional)

For high-concurrency testing, you can introduce a separate orchestration service (sister project) that:

- Receives SCM webhooks or CI callbacks.
- Enqueues review jobs and debounces by PR/head (latest `head_sha` wins).
- Starts worker processes/containers that run:
  - `code-review --owner ... --repo ... --pr ... --head-sha ...`

In that setup:

- Use sections **A** or **B** to verify the worker image/CLI locally (one review at a time).
- Use the orchestration project’s own docs (see `ORCHESTRATION_PLAN_SERVICE.md`) to:
  - Run a local instance of the webhook/queue/worker stack.
  - Verify that multiple rapid PR updates result in a **single** review for the latest head SHA.

The Python package itself remains unchanged; only how you **schedule** reviews differs.
