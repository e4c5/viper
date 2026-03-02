# Code Review Agent

AI-driven code review agent for CI/CD pipelines. Reviews pull request diffs, posts inline comments, and tracks resolved issues. Supports Gitea, GitHub, GitLab, Bitbucket and configurable LLMs (Gemini, OpenAI, Anthropic, Vertex, Ollama).

## Quick Start

- **Recommended (local testing):** Use [Docker Compose](docs/QUICKSTART.md#option-1-test-with-docker-compose-recommended) to run Gitea + Jenkins, then run the agent from the pipeline or as a one-shot container.
- **Without Docker:** [Install locally](docs/QUICKSTART.md#option-2-run-without-docker-compose) and run `code-review review --owner <owner> --repo <repo> --pr <n> --head-sha <sha>` (set `SCM_*` and `LLM_*` as environment variables; you can load them from a `.env` file using your shell, CI, or a dotenv tool).

Full steps, configuration, and one-shot Docker usage are in the **[Quick Start Guide](docs/QUICKSTART.md)**.

## Configuration

Copy `.env.example` to `.env` and set SCM (`SCM_PROVIDER`, `SCM_URL`, `SCM_TOKEN`) and LLM (`LLM_PROVIDER`, `LLM_MODEL`, and the provider API key`). Ensure your shell or CI loads these into environment variables before running the CLI. Optional: `SCM_SKIP_LABEL`, `SCM_SKIP_TITLE_PATTERN`. See [Quick Start](docs/QUICKSTART.md#configuration) and `.env.example`.

For Jenkins, store secrets in **Manage Jenkins → Credentials** as Secret text IDs (e.g. `SCM_TOKEN`, `GOOGLE_API_KEY`) and let the pipeline read them (see `docker/jenkins/Jenkinsfile`).

## Docker and CI

The agent runs as a **one-shot container** (no long-running service). Build: `docker build -t code-review-agent -f docker/Dockerfile.agent .` Run with `SCM_*` and `LLM_*` env vars; for Compose-based testing and Jenkins pipeline details, see [Quick Start](docs/QUICKSTART.md#option-1-test-with-docker-compose-recommended).

The Compose-based Jenkins image preinstalls required plugins and can auto-seed credentials for local testing during first-boot bootstrap (see Quick Start).

## Security (CI)

- Use a **least-privilege token** (repo-scoped read + comment); avoid org-wide tokens.
- Restrict **container egress** where possible (allowlist SCM and LLM API endpoints). The agent should be the only component calling SCM APIs.

## Observability (optional)

Install with `pip install -e ".[observability]"`. Set `CODE_REVIEW_METRICS=prometheus` and/or `CODE_REVIEW_TRACING=otel`; use `code_review.observability.get_prometheus_registry()` for a `/metrics` endpoint. See [Developer Guide](docs/DEVELOPER_GUIDE.md) for metrics and span names.

## Development

```bash
pip install -e ".[dev]"
pytest
```

See [Developer Guide](docs/DEVELOPER_GUIDE.md) for architecture, testing, and extension points.
