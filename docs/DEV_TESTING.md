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
