# Code Review Agent

AI-driven code review for CI/CD: reviews pull request diffs, posts inline comments, and tracks resolved issues. Supports **Gitea**, **GitHub**, **GitLab**, **Bitbucket** and configurable LLMs (Gemini, OpenAI, Anthropic, Vertex, Ollama). The agent runs as a one-shot container or CLI. No long-running service.

---

## Getting started

Choose the path that matches your setup.

| You want to… | Guide |
|--------------|--------|
| **Use your existing Jenkins** (add the agent to current CI) | **[Jenkins (existing installation)](docs/JENKINS-EXISTING.md)** — add a pipeline job, credentials, and SCM/LLM env; use Docker on agents or run the CLI inline. |
| **Try it locally** (Gitea + Jenkins via Docker Compose) | **[Quick Start (Docker)](docs/QUICKSTART.md)** — start the stack, configure Gitea and Jenkins, wire webhooks. |
| **Use Podman instead of Docker** | **[Quick Start (Podman)](docs/QUICKSTART-podman.md)** — rootless Podman and the same stack. |
| **Run Jenkins without Docker** (no containers on agents) | **[Jenkins without Docker](docs/JENKINS-NO-DOCKER.md)** — install the CLI on agents and set `USE_INLINE_AGENT=true`. |
| **Your SCM is Bitbucket Data Center** | **[Bitbucket Data Center](docs/BITBUCKET-DATACENTER.md)** — same credential `SCM_TOKEN`, Bitbucket webhook JSONPaths, and env. |
| **You use multiple SCMs** (e.g. Gitea + GitHub) | **[Jenkins with multiple SCMs](docs/JENKINS-MULTIPLE-SCMS.md)** — one folder and pipeline per SCM, same Jenkinsfile. |
| **Automate Jenkins setup** (Playwright, Jenkins 2.552) | **[E2E UI: Playwright Jenkins flows](docs/E2E-UI-JENKINS.md)** — run standalone scripts for single-SCM or multi-SCM; secrets from `.env`. |
| **Develop or test locally** (any SCM) | **[Development testing](docs/DEV_TESTING.md)** — run `code-review review` directly; **[Developer guide](docs/DEVELOPER_GUIDE.md)** — architecture and extension points. |

---

## Configuration

- **Environment**: Copy `.env.example` to `.env` and set at least:
  - **SCM**: `SCM_PROVIDER`, `SCM_URL`, `SCM_TOKEN` (and optionally `SCM_OWNER`, `SCM_REPO`, `SCM_PR_NUM`, `SCM_HEAD_SHA`, `SCM_SKIP_LABEL`, `SCM_SKIP_TITLE_PATTERN`).
  - **LLM**: `LLM_PROVIDER`, `LLM_MODEL`, and the provider API key (e.g. `GOOGLE_API_KEY`).
- **Jenkins**: Store secrets as Secret text (`SCM_TOKEN`, `GOOGLE_API_KEY`) in **Manage Jenkins → Credentials** (global) or in a **Folder’s Credentials** so only the code-review pipeline can use them. Set `SCM_PROVIDER` and `SCM_URL` in the job or global env. See [Jenkins (existing)](docs/JENKINS-EXISTING.md) and [Quick Start](docs/QUICKSTART.md#configuration).

---

## Running the agent

- **Container (Docker/Podman)**  
  - Prebuilt: `docker pull e4c5/code-review-agent` then e.g. `docker tag e4c5/code-review-agent code-review-agent`.  
  - Build: `docker build -t code-review-agent -f docker/Dockerfile.agent .`  
  Run with `SCM_*` and `LLM_*` (and `GOOGLE_API_KEY`) set in the environment.
- **CLI (no container)**  
  Install with `pip install -e .` (or from wheel/PyPI), then:  
  `code-review review --owner <owner> --repo <repo> --pr <n> --head-sha <sha>`  
  Same env vars; see [Jenkins without Docker](docs/JENKINS-NO-DOCKER.md) for Jenkins inline usage.

---

## Security (CI)

- Use a **least-privilege** SCM token (repo-scoped read + comment); avoid org-wide tokens.
- Restrict container egress where possible (allowlist SCM and LLM API endpoints).

---

## Observability (optional)

```bash
pip install -e ".[observability]"
```

Set `CODE_REVIEW_METRICS=prometheus` and/or `CODE_REVIEW_TRACING=otel`. Use `code_review.observability.get_prometheus_registry()` for a `/metrics` endpoint. See [Developer guide](docs/DEVELOPER_GUIDE.md).

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

See [Developer guide](docs/DEVELOPER_GUIDE.md) for architecture, testing, and extension points.
