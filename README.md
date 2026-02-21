# Code Review Agent

AI-driven code review agent for CI/CD pipelines. Reviews pull request diffs, posts inline comments, and tracks resolved issues. Supports Gitea (with extensibility for GitLab, Bitbucket) and configurable LLMs (Gemini, OpenAI, Ollama).

## Quick Start

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS; on Windows: .venv\Scripts\activate

# Install
pip install -e .

# Run (requires SCM_* and LLM env vars)
code-review review --owner myorg --repo myrepo --pr 42 --head-sha <sha>
```

## Configuration

Copy `.env.example` to `.env` and set:

- `SCM_PROVIDER`, `SCM_URL`, `SCM_TOKEN` — SCM access
- `LLM_PROVIDER`, `LLM_MODEL` — LLM (gemini, openai, anthropic, ollama)
- `SCM_SKIP_LABEL` (optional) — PR label to skip review (e.g. `skip-review`)
- `SCM_SKIP_TITLE_PATTERN` (optional) — if PR title contains this, skip (e.g. `[skip-review]`)

## Docker and CI

The agent runs as a one-shot container (no long-running service):

```bash
# Build from repo root
docker build -t code-review-agent -f docker/Dockerfile.agent .

# Run (pass SCM_* and LLM_* via env)
docker run --rm -e SCM_URL=... -e SCM_TOKEN=... -e SCM_OWNER=... -e SCM_REPO=... -e SCM_PR_NUM=... -e SCM_HEAD_SHA=... -e LLM_PROVIDER=gemini -e GOOGLE_API_KEY=... code-review-agent review
```

For local testing, use Docker Compose (Gitea + Jenkins):

```bash
docker compose up -d
# Configure Gitea and Jenkins; trigger pipeline with SCM_* from webhook. See docker/jenkins/Jenkinsfile.
```

If you run compose with a different project name (`docker compose -p myproject up`), set `COMPOSE_PROJECT_NAME=myproject` in Jenkins (or in a `.env` at repo root if Jenkins loads it) so the pipeline uses the correct Docker network (`${COMPOSE_PROJECT_NAME}_code-review-net`). The default project name is `code-review` (set in `docker-compose.yml`).

## Security (CI)

- **Least-privilege token:** Use a bot account with repo-scoped (read + comment) permission only; avoid org-wide tokens.
- **Container networking:** Run the agent container with restricted egress where possible (allowlist SCM_URL and LLM API endpoints). The agent is the only component that should call SCM APIs; avoid passing credentials to plugins that also talk to SCM.

## Observability (optional)

To export metrics and traces:

```bash
pip install -e ".[observability]"
```

- **Prometheus:** set `CODE_REVIEW_METRICS=prometheus` (or `CODE_REVIEW_PROMETHEUS=1`). Use `code_review.observability.get_prometheus_registry()` to expose a `/metrics` endpoint (e.g. with your WSGI/ASGI server).
- **OpenTelemetry:** set `CODE_REVIEW_TRACING=otel` (or `CODE_REVIEW_OTEL=1`). Set `OTEL_EXPORTER_OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) to export spans (e.g. `http://localhost:4318/v1/traces`).

Metrics: `code_review_runs_total{outcome}`, `code_review_run_duration_seconds`, `code_review_findings_total`, `code_review_posts_total`. Spans: `run_review` with attributes (owner, repo, pr_number, files_count, findings_count, posts_count, duration_seconds).

## Development

```bash
source .venv/bin/activate   # Use project venv
pip install -e ".[dev]"
pytest
```
