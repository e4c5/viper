## Code Review Agent

AI-driven code review agent for CI/CD pipelines. Reviews pull request diffs, posts inline comments, and tracks resolved issues. Supports Gitea, GitHub, GitLab, Bitbucket and configurable LLMs (Gemini, OpenAI, Anthropic, Vertex, Ollama).

---

## Usage at a glance

- **Run via Jenkins + Docker (recommended for local testing)**: Bring up Gitea + Jenkins with Docker Compose and let PR webhooks trigger reviews.
- **Run without Docker**: Install the CLI on a node and call `code-review review` directly from CI.

---

## Choose your path

- **Quick Start (Docker, Gitea, Jenkins)**  
  See **[Quick Start Guide](docs/QUICKSTART.md)** for:
  - Starting the Docker/Compose stack.
  - Configuring Gitea and Jenkins credentials.
  - Wiring webhooks so PRs auto-trigger reviews.

- **Quick Start – Podman (rootless)**  
  Use **[Quick Start – Podman](docs/QUICKSTART-podman.md)** if you prefer Podman instead of Docker. This covers rootless Podman setup and how to run the same stack with `podman` / `podman-compose`.

- **Jenkins without Docker (inline agent)**  
  Use **[Jenkins without Docker](docs/JENKINS-NO-DOCKER.md)** to:
  - Install the `code-review` CLI on Jenkins agents.
  - Run the pipeline in “inline” mode (`USE_INLINE_AGENT=true`) when container runtimes are not available or not allowed.

- **Local / ad‑hoc testing (any SCM)**  
  See **[Development Testing Guide](docs/DEV_TESTING.md)** for:
  - Local testing paths with Docker or without Docker.
  - Running `code-review review --owner <owner> --repo <repo> --pr <n> --head-sha <sha>` directly against Gitea, GitHub, GitLab, or Bitbucket.

- **Bitbucket Data Center**  
  Use a **separate** Jenkins job; **[Bitbucket Data Center](docs/BITBUCKET-DATACENTER.md)** for:
  - Same `Jenkinsfile` as Gitea/GitHub; credential `SCM_TOKEN_BITBUCKET`, env `SCM_URL`, and Bitbucket webhook URL.
  - JSONPath mappings and event filter for Bitbucket PR payloads.

- **Architecture and extension points**  
  See **[Developer Guide](docs/DEVELOPER_GUIDE.md)** for:
  - High‑level architecture and module layout.
  - Provider interface and how to add new SCMs.
  - Agent behavior, models, observability, and tests.

---

## Configuration

- Copy `.env.example` to `.env` and set:
  - **SCM**: `SCM_PROVIDER`, `SCM_URL`, `SCM_TOKEN` (and optionally `SCM_OWNER`, `SCM_REPO`, `SCM_PR_NUM`, `SCM_HEAD_SHA`, `SCM_SKIP_LABEL`, `SCM_SKIP_TITLE_PATTERN`).
  - **LLM**: `LLM_PROVIDER`, `LLM_MODEL`, and the provider API key.
- Ensure your shell or CI loads these into environment variables before running the CLI.  
  See **[Quick Start](docs/QUICKSTART.md#configuration)** and `.env.example` for details.

For Jenkins, store secrets in **Manage Jenkins → Credentials** as Secret text (`SCM_TOKEN`, `GOOGLE_API_KEY`). Use a **separate pipeline job** for Bitbucket Data Center (different Jenkinsfile and credential); see **[Bitbucket Data Center](docs/BITBUCKET-DATACENTER.md)**. For inline execution without Docker, set `USE_INLINE_AGENT=true` and follow **[docs/JENKINS-NO-DOCKER.md](docs/JENKINS-NO-DOCKER.md)**.

---

## Docker and CI

The agent runs as a **one-shot container** (no long‑running service):

- **Prebuilt image (Docker Hub)**: pull `e4c5/code-review-agent` from Docker Hub (`docker pull e4c5/code-review-agent`), then optionally retag as `code-review-agent` so examples work unchanged.
- **Build locally**: `docker build -t code-review-agent -f docker/Dockerfile.agent .`.

Run containers with `SCM_*` and `LLM_*` environment variables set; for Compose-based testing and Jenkins pipeline details, see **[Quick Start](docs/QUICKSTART.md)** and **[Quick Start – Podman](docs/QUICKSTART-podman.md)**.

The Compose-based Jenkins image preinstalls required plugins and can auto‑seed credentials for local testing during first‑boot bootstrap (see Quick Start).

---

## Security (CI)

- **Tokens**: Use a least‑privilege token (repo‑scoped read + comment); avoid org‑wide tokens.
- **Network**: Restrict container egress where possible (allowlist SCM and LLM API endpoints). The agent should be the only component calling SCM APIs.

---

## Observability (optional)

Install with:

```bash
pip install -e ".[observability]"
```

Then set `CODE_REVIEW_METRICS=prometheus` and/or `CODE_REVIEW_TRACING=otel`, and use `code_review.observability.get_prometheus_registry()` for a `/metrics` endpoint. See **[Developer Guide](docs/DEVELOPER_GUIDE.md)** for metrics and span names.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

See **[Developer Guide](docs/DEVELOPER_GUIDE.md)** for architecture, testing strategy, and extension points.
