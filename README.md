# Code Review Agent

AI-driven code review for CI/CD: reviews pull request diffs, posts inline comments, and tracks resolved issues. Supports **Gitea**, **GitHub**, **GitLab**, **Bitbucket** and configurable LLMs (Gemini, OpenAI, Anthropic, Vertex etc and local models with Ollama). The agent runs as a one-shot container or CLI. No long-running service.

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
| **Use GitHub Codespaces** | **[GitHub Codespaces](docs/GITHUB-CODESPACES.md)** — open the repo in a Codespace, use inline mode by default, and run the local Docker Compose stack with the Codespaces override file. |
| **Develop or test locally** (any SCM) | **[Development testing](docs/DEV_TESTING.md)** — run `code-review` directly; **[Developer guide](docs/DEVELOPER_GUIDE.md)** — architecture and extension points. |
| **Run in GitHub Actions** (GitHub‑hosted CI) | **[GitHub Actions](docs/GITHUB-ACTIONS.md)** — full container-based setup guide for running the review agent on pull requests. |

---

## Configuration

**Local runs.** When you run `code-review` on your machine or start the container locally, set:

- **SCM** — provider, URL, and token  
- **LLM** — provider, model, and API key  

Copy `.env.example` to `.env` and fill in the values; that file documents every variable.

**Reference:** [Configuration reference](docs/CONFIGURATION-REFERENCE.md) — full list of `SCM_*`, `LLM_*`, `CONTEXT_*`, `CODE_REVIEW_*`, observability, and related options.

**CI and Jenkins.** Pipelines usually inject secrets as credentials mapped to job or global environment variables. You typically do **not** rely on a project `.env` on the agent. See [Jenkins (existing)](docs/JENKINS-EXISTING.md) and [Quick Start — Configuration](docs/QUICKSTART.md#configuration).

**Context-aware review (optional).** If you turn this on, the runner can load linked GitHub Issues, Jira tickets, or Confluence pages, summarize them into a short brief, and attach that (and optionally PR commit messages) to the review prompt. It is **off by default**. Enabling it requires PostgreSQL with pgvector. See [Context-aware user guide](docs/CONTEXT-AWARE-USER-GUIDE.md) and [Context-aware developer guide](docs/CONTEXT-AWARE-DEVELOPER-GUIDE.md).

**Auto review decision (optional)** — You can enable automatic PR review decisions on SCMs that support them. Set `SCM_REVIEW_DECISION_ENABLED=true` to submit `REQUEST_CHANGES` when **aggregated open** high/medium signals meet thresholds (`SCM_REVIEW_DECISION_HIGH_THRESHOLD`, `SCM_REVIEW_DECISION_MEDIUM_THRESHOLD`), otherwise submit `APPROVE` (for example when only low/nit remain). Counts combine **net-new findings from this run** with **already-unresolved review items** from the SCM (deduped by fingerprint marker when present). Severity for existing threads is inferred from `[High]` / `[Medium]` / … in comment text where possible. **Merge blocking** depends on each host’s branch protection or merge checks, not on the API call alone; see **[SCM review decisions and merge blocking](docs/SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md)** for GitHub, Gitea, GitLab, Bitbucket Cloud, and Bitbucket Data Center.

**Reply-dismissal (optional, review-decision-only)** — When you run **`--review-decision-only`** with webhook context (`CODE_REVIEW_EVENT_*`) and set **`CODE_REVIEW_REPLY_DISMISSAL_ENABLED=true`**, a reply on a review thread can be classified so **agreed** threads are excluded from the quality gate for that run; **disagreed** may post a follow-up on **GitHub** or **GitLab** only. See [Configuration reference](docs/CONFIGURATION-REFERENCE.md) §5.

Per-SCM “open” semantics:

| SCM | What counts as unresolved for the gate |
|-----|----------------------------------------|
| **GitHub** | Pull request review threads that are not resolved and not outdated (via GraphQL `reviewThreads`; falls back to REST review comments if GraphQL fails). |
| **Gitea** | Review comments with `resolved=false` from the API (when exposed). |
| **GitLab** | Merge request discussions with `resolved=false` (thread-level; severity is the max across diff notes in the thread). |
| **Bitbucket Cloud** | Open PR **tasks** only (inline comments do not expose resolved state in the API). |
| **Bitbucket Server/DC** | Comments that are not `RESOLVED` plus open PR **tasks**. |

---

## Running the agent

- **Container (Docker/Podman)**  
  - Prebuilt: `docker pull e4c5/code-review-agent` then e.g. `docker tag e4c5/code-review-agent code-review-agent`.  
  - Build: `docker build -t code-review-agent -f docker/Dockerfile.agent .`  
  Run with `SCM_*` and `LLM_*` (including `LLM_API_KEY`) set in the environment.
- **CLI (no container)**  
  Install with `pip install -e .` (or from wheel/PyPI), then:  
  `code-review --owner <owner> --repo <repo> --pr <n> --head-sha <sha>`  
  Same env vars; see [Jenkins without Docker](docs/JENKINS-NO-DOCKER.md) for Jenkins inline usage.
  Decision thresholds can also be overridden per run with
  `--review-decision-enabled`, `--review-decision-high-threshold`, and
  `--review-decision-medium-threshold`.

**Log level**  
By default the CLI is quiet. To see progress (files fetched, agent run, comments posted), set the log level before running:

| Level   | When to use |
|--------|-------------|
| `INFO` | See progress messages (recommended when debugging). |
| `DEBUG`| Verbose output. |
| `WARNING` | Default; only warnings and errors. |

**Examples:**

```bash
# Progress messages (e.g. "Fetched diff, 3 files", "Posted 2 comments")
CODE_REVIEW_LOG_LEVEL=INFO code-review --owner myorg --repo myrepo --pr 5 --head-sha abc123

# Or put in .env and source it
# CODE_REVIEW_LOG_LEVEL=INFO
source .env
code-review --owner myorg --repo myrepo --pr 5 --head-sha abc123
```

See [Developer guide §6.3 Logging](docs/DEVELOPER_GUIDE.md#63-logging) for details.

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
