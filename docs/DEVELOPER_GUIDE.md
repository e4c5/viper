# Code Review Agent — Developer Guide

This guide explains the implementation of the AI-driven code review agent: architecture, data flow, key modules, configuration, and how to extend or test the system.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Project Structure](#3-project-structure)
4. [End-to-End Flow](#4-end-to-end-flow)
5. [Key Modules](#5-key-modules)
6. [Configuration and Environment](#6-configuration-and-environment)
7. [Extension Points](#7-extension-points)
8. [Testing](#8-testing)
9. [References](#9-references)
10. [Development Testing](#10-development-testing)

---

## 1. Overview

The code review agent:

- **Input**: A pull request (owner, repo, PR number, optional head SHA).
- **Process**: Fetches the PR diff and context via an SCM provider, runs a Google ADK agent (LLM + tools) to produce structured findings, then filters and posts inline comments (and optionally a PR summary) via the same provider.
- **Output**: Inline review comments on the PR; programmatic callers receive a list of `FindingV1` that were (or would be) posted.

Design principles:

- **Findings-only agent**: The LLM agent only discovers issues and returns a JSON array of findings. The **runner** (Python) handles fetching existing comments, building an ignore set, fingerprinting, idempotency, and posting. This keeps the agent focused and avoids giving the LLM access to “post” or “resolve” tools in the default mode.
- **Provider-agnostic**: SCM access is behind `ProviderInterface`; concrete implementations exist for Gitea, GitHub, GitLab, and Bitbucket.
- **LLM-agnostic**: The model is chosen via config (Gemini, OpenAI, Anthropic, Ollama, Vertex); the agent uses Google ADK’s `Agent` and model factory.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  CLI (__main__.py)                                                       │
│  code-review --owner X --repo Y --pr N [--head-sha SHA]                   │
└─────────────────────────────────────┬───────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Runner (runner.py)                                                      │
│  • Load config (SCM + LLM)                                              │
│  • get_provider() → ProviderInterface                                   │
│  • Skip review? (label / title pattern)                                  │
│  • Fetch existing comments → build ignore set                           │
│  • Idempotency check (run id in comment markers)                        │
│  • get_pr_files → detect language → get_review_standards                 │
│  • create_review_agent(provider, review_standards, findings_only=True)  │
│  • ADK Runner + InMemorySessionService                                  │
│  • If diff too large: loop per file; else single run                    │
│  • runner.run() → collect response text → _findings_from_response()      │
│  • Filter findings (ignore set, fingerprint)                            │
│  • Format bodies, add fingerprint marker → post_review_comments         │
│  • post_pr_summary_comment (then observability.finish_run)              │
└─────────────────────────────────────┬───────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           ▼
┌──────────────────┐    ┌────────────────────────────┐   ┌─────────────────┐
│  Provider        │    │  ADK Runner                 │   │  Observability  │
│  (Gitea/GitHub/  │    │  • Agent (model, tools,     │   │  start_run /    │
│   GitLab/        │    │    instruction)              │   │  finish_run    │
│   Bitbucket)     │    │  • Session                  │   │  (Prometheus /   │
│  get_pr_diff,    │    │  • run(user_id, session_id, │   │  OTel optional) │
│  get_pr_files,   │    │    new_message)              │   │                 │
│  post_review_    │    │  → LLM + tool calls          │   │                 │
│  comments, etc. │    │  → final response (JSON)     │   │                 │
└──────────────────┘    └────────────────────────────┘   └─────────────────┘
```

- **CLI** is the only user-facing entry point; it delegates to `run_review()`.
- **Runner** owns orchestration: config, provider, skip/idempotency, language detection, review standards, agent creation, ADK run(s), parsing, filtering, and posting.
- **Agent** is an ADK `Agent` (LlmAgent) with tools that call the **provider** (get diff, get file content, etc.). The agent does **not** call `post_review_comments` or `get_existing_review_comments` in findings-only mode.
- **Provider** is the only component that talks to the SCM API (HTTP). Observability is optional and used only inside the runner.

---

## 3. Project Structure

```
src/code_review/
├── __init__.py
├── __main__.py              # CLI: Typer app → run_review()
├── config.py                 # SCMConfig, LLMConfig, ContextConfig (Pydantic Settings); get_scm_config(), get_llm_config(), get_context_config()
├── models.py                 # get_configured_model(), get_context_window(), get_max_output_tokens()
├── runner.py                 # run_review(); orchestration and ADK Runner
├── observability.py          # Optional Prometheus/OTel; start_run(), finish_run(), get_prometheus_registry()
├── agent/
│   ├── __init__.py           # create_review_agent
│   ├── agent.py              # create_review_agent(provider, review_standards, findings_only, pr_context)
│   └── tools/
│       ├── __init__.py
│       ├── gitea_tools.py    # create_gitea_tools(), create_findings_only_tools() — wrap ProviderInterface
│       └── review_helpers.py # detect_language_context() — agent tool
├── context/
│   ├── __init__.py           # Re-exports ContextFetcher, Reference, ReferenceType, extract_references
│   ├── extractor.py          # extract_references() — regex extraction of GitHub/Jira/Confluence refs
│   ├── fetcher.py            # ContextFetcher.build_context() — orchestrate fetch + token budget
│   ├── github_issues.py      # GitHubIssuesFetcher — fetch issue title, body, labels, comments
│   ├── jira.py               # JiraFetcher — fetch Jira ticket fields and comments (plain text + ADF)
│   └── confluence.py         # ConfluenceFetcher — fetch Confluence page by ID or URL; HTML→plain text
├── providers/
│   ├── __init__.py           # get_provider(name, base_url, token); exports base types
│   ├── base.py               # ProviderInterface, InlineComment, ReviewComment, FileInfo, PRInfo, CommitInfo, ProviderCapabilities
│   ├── safety.py             # truncate_repo_content()
│   ├── gitea.py              # GiteaProvider
│   ├── github.py             # GitHubProvider (implements get_pr_commits())
│   ├── gitlab.py             # GitLabProvider
│   └── bitbucket.py          # BitbucketProvider
├── schemas/
│   └── findings.py          # FindingV1 (path, line, severity, code, message, ...)
├── diff/
│   ├── parser.py             # parse_unified_diff(), DiffHunk, iter_new_lines()
│   ├── position.py          # get_commentable_positions(), position_for_line(), CommentablePosition
│   └── fingerprint.py       # build_fingerprint(), surrounding_content_hash(), format_comment_body_with_marker(), parse_marker_from_comment_body()
├── standards/
│   ├── __init__.py           # detect_from_paths(), detect_from_paths_and_content(), get_review_standards()
│   ├── detector.py          # Language/framework from paths and optional content
│   └── prompts/
│       ├── base.py          # BASE_REVIEW_PROMPT
│       └── ...              # Per-language fragments (python, js, go, etc.)
└── formatters/
    └── comment.py           # finding_to_comment_body(f) → "[Severity] message"
```

---

## 4. End-to-End Flow

### 4.1 CLI to Runner

1. User runs: `code-review --owner myorg --repo myrepo --pr 42 --head-sha abc123` (or sets `SCM_OWNER`, `SCM_REPO`, `SCM_PR_NUM`, `SCM_HEAD_SHA`).
2. `__main__.py` resolves owner, repo, pr, head_sha from options or env; validates; calls `run_review(owner, repo, pr_number, head_sha, dry_run=..., print_findings=...)`.

### 4.2 Runner Steps (summary)

| Step | What happens |
|------|-------------------------------|
| 1 | Generate `trace_id`; call `observability.start_run(trace_id)`. |
| 2 | Load `get_scm_config()`, `get_llm_config()`; instantiate `get_provider(cfg.provider, cfg.url, cfg.token)`. |
| 3 | **Skip review**: `provider.get_pr_info(owner, repo, pr_number)`; if skip label or title pattern matches → log, `finish_run`, return `[]`. |
| 4 | **Existing comments**: `provider.get_existing_review_comments(...)`; build `ignore_set` with `_build_ignore_set()` (path + body hash, path + fingerprint from marker). |
| 5 | **Idempotency**: If `head_sha` is set, build `run_id` from provider/owner/repo/pr/head_sha/agent_version/config_hash. If any existing comment body contains this run id in the marker → skip run, return `[]`. |
| 6 | **PR metadata + context enrichment** (opt-in): `provider.get_pr_info(...)` for title/description; if `CONTEXT_ENABLED=true`, `_fetch_pr_context()` calls `provider.get_pr_commits(...)`, extracts GitHub/Jira/Confluence references from the PR title, description, and commit messages, fetches their content, and assembles a `<context>…</context>` string for the prompt. |
| 7 | **PR files**: `provider.get_pr_files(owner, repo, pr_number)` → list of paths. |
| 8 | **Language & standards**: `detect_from_paths(paths)` → `DetectedContext`; `get_review_standards(language, framework)` → string appended to agent instruction. |
| 9 | **Agent**: `create_review_agent(provider, review_standards, findings_only=True, pr_context=pr_context)` → ADK `Agent` with model, instruction (extended with `_CONTEXT_INSTRUCTION` when `pr_context` is non-empty), tools, `generate_content_config`. |
| 10 | **Session**: `InMemorySessionService()`; create session with `app_name`, `user_id`, `session_id`. |
| 11 | **Runner**: `Runner(agent=agent, app_name=..., session_service=...)`. |
| 12 | **Token budget**: `diff_budget = get_context_window() * DIFF_TOKEN_BUDGET_RATIO`; `full_diff = provider.get_pr_diff(...)`; if `_estimate_tokens(full_diff) > diff_budget` and there are paths → **file-by-file**: for each path, build user message “Review this PR … Review only this file: {path}” (appending `pr_context` when present), call `runner.run(..., new_message=content)`, collect response, parse findings; else single message “Review this PR …” (appending `pr_context` when present), one `runner.run()`, parse findings. |
| 13 | **Parse**: `_findings_from_response(response_text)` extracts JSON array and maps to `FindingV1` (invalid entries skipped). |
| 14 | **Filter**: For each finding, compute comment body and body hash; optionally `_fingerprint_for_finding()` using file lines at `head_sha`. Skip if `(path, body_hash)` or `(path, fingerprint)` in `ignore_set`; else append to `to_post`. |
| 15 | **Optional**: If `print_findings`, print each finding to stdout. |
| 16 | **Post** (if not dry_run and `to_post`): Require `head_sha`. For each finding, build `body` with `finding_to_comment_body()` and `format_comment_body_with_marker(fingerprint, version, run_id)`. Build list of `InlineComment`; call `provider.post_review_comments(...)`. On exception, fall back to posting one-by-one; on per-comment failure, post that finding as PR-level summary. Then `provider.post_pr_summary_comment(...)` with summary body. |
| 17 | **Finish**: `_log_run_complete(...)`; `observability.finish_run(run_handle, ...)`; return list of findings that were (or would be) posted. |

### 4.3 Where the LLM Is Used

All LLM calls occur inside **ADK’s `Runner.run()`**. The runner only:

- Builds the user message (PR context and optionally “Review only this file: path”).
- Calls `runner.run(user_id, session_id, new_message=content)`.
- Consumes the event stream and concatenates final response text.

The agent (inside ADK) uses `get_configured_model()` for the model and calls tools (e.g. `get_pr_diff`, `get_pr_diff_for_file`, `get_file_content`, `get_file_lines`, `get_pr_files`, `detect_language_context`), which in turn call the **provider**. So the provider is used both by the runner (get existing comments, get files, get diff, get file content for fingerprinting, post comments) and by the agent via tools (get diff, get file content, etc.).

---

## 5. Key Modules

### 5.1 `config.py`

- **SCMConfig**: `env_prefix="SCM_"`. Fields: `provider`, `url`, `token`, `owner`, `repo`, `pr_num`, `head_sha`, `base_sha`, `event`, `skip_label`, `skip_title_pattern`.
- **LLMConfig**: `env_prefix="LLM_"`. Fields: `provider`, `model`, `context_window`, `max_output_tokens`, `temperature`, `disable_tool_calls`, `timeout_seconds`, `max_retries`.
- **ContextConfig**: `env_prefix="CONTEXT_"`. Fields: `enabled`, `github_issues_enabled`, `jira_enabled`, `jira_url`, `jira_email`, `jira_token`, `jira_project_keys`, `confluence_enabled`, `confluence_url`, `confluence_email`, `confluence_token`, `max_context_tokens`. Disabled by default.
- **get_scm_config()**, **get_llm_config()**, **get_context_config()**: Cached so config is read once per process.

### 5.2 `models.py`

- **get_configured_model()**: Returns the ADK model: for `gemini`/`vertex` a string (e.g. `gemini-2.5-flash`); for `openai`/`anthropic`/`ollama` a `LiteLlm(model="provider/model")` instance. Uses `get_llm_config()`.
- **get_context_window()**, **get_max_output_tokens()**: Read from `LLMConfig`; used by runner (chunking) and by agent (via `generate_content_config` in `agent.py`).

### 5.3 `agent/agent.py`

- **create_review_agent(provider, review_standards="", findings_only=True, *, pr_context="")**:
  - If `findings_only`: tools from `create_findings_only_tools(provider)` (get_pr_diff, get_pr_diff_for_file, get_file_content, get_file_lines, get_pr_files, detect_language_context); instruction = `FINDINGS_ONLY_INSTRUCTION`.
  - Else: tools from `create_gitea_tools(provider)` (adds post_review_comment, get_existing_review_comments); instruction = `BASE_INSTRUCTION`.
  - Appends `review_standards` to instruction.
  - If `pr_context` is non-empty, appends `_CONTEXT_INSTRUCTION` to the instruction so the LLM knows how to use the context block.
  - Builds `generate_content_config` from `get_llm_config()` (temperature, max_output_tokens).
  - Returns ADK `Agent(model=get_configured_model(), name="code_review_agent", instruction=..., tools=..., generate_content_config=...)`.

### 5.4 `agent/tools/gitea_tools.py`

- **create_gitea_tools(provider)** / **create_findings_only_tools(provider)**: Return lists of callables that close over `provider`. Each callable delegates to the corresponding `ProviderInterface` method (e.g. `get_pr_diff` → `provider.get_pr_diff(...)`). ADK treats these as function tools and wraps them automatically.
- **review_helpers.detect_language_context**: Agent tool that calls `detect_from_paths` / `detect_from_paths_and_content` from `standards` for ambiguous language detection.

### 5.5 `providers/`

- **base.py**: Defines `ProviderInterface` (ABC) and shared types: `InlineComment`, `ReviewComment`, `FileInfo`, `PRInfo`, `CommitInfo`, `ProviderCapabilities`. All concrete providers implement the same interface. The `get_pr_commits()` method returns a list of `CommitInfo` (sha + message) for context enrichment; the base implementation returns `[]` and GitHub overrides it.
- **get_provider(name, base_url, token)**: Returns `GiteaProvider` | `GitHubProvider` | `GitLabProvider` | `BitbucketProvider` for `name` in `gitea` | `github` | `gitlab` | `bitbucket`.
- **safety.py**: `truncate_repo_content(content, max_bytes)` used when feeding repo file content (e.g. README, AGENTS.md) to the agent to avoid unbounded context.

### 5.6 `schemas/findings.py`

- **FindingV1**: Pydantic model for one finding: `path`, `line`, `end_line`, `severity`, `code`, `message`, `body`, `category`, `anchor`, `fingerprint_hint`, `version`. Used to parse the agent’s JSON output and to represent “findings to post.” `get_body()` returns the comment text (body or message).

### 5.7 `diff/`

- **parser.py**: `parse_unified_diff(diff_text)` → list of `DiffHunk` (path, old/new start/count, lines). Used by providers for `get_pr_diff_for_file` and by position/fingerprint logic.
- **position.py**: `get_commentable_positions(hunks)` and `position_for_line()` map (path, line) to diff positions for provider-specific comment placement where needed.
- **fingerprint.py**: `surrounding_content_hash()`, `build_fingerprint()`, `format_comment_body_with_marker()`, `parse_marker_from_comment_body()`. The marker is a hidden HTML comment in each posted comment body (e.g. `<!-- code-review-agent:fingerprint=...;version=...;run=... -->`) for dedup and idempotency.

### 5.8 `standards/`

- **detector.py**: `detect_from_paths(paths)` and `detect_from_paths_and_content(paths, content_map)` return `DetectedContext` (language, framework, confidence). Used by runner and by the `detect_language_context` tool.
- **prompts/**: `get_review_standards(language, framework)` returns a string (base prompt + per-language fragment) appended to the agent instruction.

### 5.9 `formatters/comment.py`

- **finding_to_comment_body(f)**: Maps a `FindingV1` to the comment text shown in the PR (e.g. `[Critical]` / `[Suggestion]` / `[Info]` + message). Used when building bodies for posting and when computing body hashes for the ignore set.

### 5.10 `observability.py`

- Optional. When `CODE_REVIEW_METRICS=prometheus` (or similar) or `CODE_REVIEW_TRACING=otel` is set and optional deps are installed: **start_run(trace_id)**, **finish_run(run_handle, owner, repo, pr_number, files_count, findings_count, posts_count, duration_seconds)**. Can expose Prometheus metrics and/or OTel spans. Runner calls these at start and end of each run.


### 5.11 `context/`

Opt-in context enrichment — disabled by default; enabled by `CONTEXT_ENABLED=true`.

- **extractor.py**: `extract_references(text, owner, repo, jira_project_keys)` scans arbitrary text
  for GitHub issue refs (`#N`, `GH-N`, full GitHub URLs), Jira ticket keys (`PROJ-NNN`, Jira browse
  URLs), and Confluence page URLs. Returns a deduplicated `list[Reference]`.
- **fetcher.py**: `ContextFetcher.build_context(pr_title, pr_description, commit_messages)` orchestrates
  reference extraction, fetching, deduplication, and token-budget enforcement. Returns a
  `<context>…</context>` string ready for injection into the LLM prompt, or empty string.
- **github_issues.py**: `GitHubIssuesFetcher` — fetches issue title, state, labels, body, and comments
  via the GitHub Issues REST API. Reuses `SCM_TOKEN`; no extra credentials needed.
- **jira.py**: `JiraFetcher` — fetches Jira ticket summary, description (plain text or Atlassian
  Document Format), issue type, status, priority, labels, and comments via Jira REST API v2.
- **confluence.py**: `ConfluenceFetcher` — fetches Confluence pages by numeric ID or URL (supports
  `/wiki/spaces/SPACE/pages/{id}/…` and `/wiki/display/SPACE/Title` patterns); strips HTML to plain
  text. Credential-based Basic Auth (compatible with both Cloud and Server/DC).

See [CONTEXT-AWARE-REVIEW.md](CONTEXT-AWARE-REVIEW.md) for the full feature guide.

---

## 6. Configuration and Environment

Configuration is read via **Pydantic Settings** in `config.py`; no `.env` file is loaded by default (env vars must be set in the process or loaded by the caller).

### 6.1 SCM (`SCM_` prefix)

| Variable | Required | Description |
|----------|----------|-------------|
| `SCM_PROVIDER` | No (default: gitea) | `gitea` \| `github` \| `gitlab` \| `bitbucket` \| `bitbucket_server` |
| `SCM_URL` | Yes | API base URL (e.g. `https://api.github.com`, `http://gitea:3000`) |
| `SCM_TOKEN` | Yes | API token for the SCM |
| `SCM_OWNER` | No | Owner/workspace/project key (provider-specific; see below). Can be passed via CLI `--owner`. |
| `SCM_REPO` | No | Repo name (can be passed via CLI `--repo`) |
| `SCM_PR_NUM` | No | PR number (can be passed via CLI `--pr`) |
| `SCM_HEAD_SHA` | No | Head commit SHA (can be passed via CLI `--head-sha`); required when posting comments |
| `SCM_BASE_SHA` | No | Base commit SHA |
| `SCM_SKIP_LABEL` | No | If PR has this label, skip review (default `skip-review`; empty = disabled) |
| `SCM_SKIP_TITLE_PATTERN` | No | If PR title contains this, skip review (default `[skip-review]`) |

**Provider-specific meaning of owner / repo:** The CLI and config use a single `--owner` and `--repo` for all providers, but their meaning depends on the SCM:

| Provider | `owner` (e.g. `SCM_OWNER` / `--owner`) | `repo` (e.g. `SCM_REPO` / `--repo`) |
|----------|----------------------------------------|-------------------------------------|
| Gitea, GitHub, GitLab | Repository owner (user or organization name) | Repository name |
| Bitbucket Cloud | **Workspace** (team or user namespace in Bitbucket Cloud) | Repo slug |
| Bitbucket Data Center (`bitbucket_server`) | **Project key** (e.g. `AN` from `project.key` in the API) | Repo slug |

For Bitbucket Data Center webhooks, `SCM_OWNER` is typically set from `$.pullRequest.toRef.repository.project.key` and `SCM_REPO` from `$.pullRequest.toRef.repository.slug`. See [Bitbucket Data Center](BITBUCKET-DATACENTER.md).

**Bitbucket Server inline comments:** For comments to appear on the correct line in the Diff view (rather than at file level), the anchor must include `lineType` matching the line in the diff: `ADDED` for lines that are added (`+` in the diff), `CONTEXT` for unchanged lines. The runner sets `line_type` per comment from the PR diff so Bitbucket can place them correctly. If a comment still appears at file level, the server may have rejected the anchor (e.g. path/ref mismatch or API version differences).

### 6.2 LLM (`LLM_` prefix)

| Variable | Required | Description |
|----------|----------|-------------|
| `LLM_PROVIDER` | No (default: gemini) | `gemini` \| `openai` \| `anthropic` \| `ollama` \| `vertex` \| `openrouter` |
| `LLM_MODEL` | No | Model name (e.g. `gemini-2.5-flash`) |
| `LLM_API_KEY` | Yes (except Ollama) | Single API key for the configured provider; the app maps it to the provider-specific env var internally. |
| `LLM_CONTEXT_WINDOW` | No | Context size in tokens (default 128000) |
| `LLM_MAX_OUTPUT_TOKENS` | No | Max output tokens (default 4096) |
| `LLM_TEMPERATURE` | No | 0 or low for deterministic review (default 0.0) |
| `LLM_DISABLE_TOOL_CALLS` | No | Debug: disable tool calls |
| `LLM_TIMEOUT_SECONDS` | No | Per-request timeout (default 60.0) |
| `LLM_MAX_RETRIES` | No | Max retries (default 3) |

> **Note (timeouts & retries)**  
> The current Google ADK Python APIs used by this project do not yet expose a straightforward way to wire `LLM_TIMEOUT_SECONDS` and `LLM_MAX_RETRIES` through to the underlying LLM client. These fields are therefore **configuration-only** for now and reserved for future use, as described in the Plan’s Phase 1 “Timeouts + retry” section.

For Ollama, `OLLAMA_API_BASE` (default `http://localhost:11434`) is used; no API key is required.

### 6.3 Logging

Log level is controlled by **`CODE_REVIEW_LOG_LEVEL`**. Default is `WARNING` (quiet). Valid values (case-insensitive): `DEBUG`, `INFO`, `WARNING`, `ERROR`.

**How to run with different log levels**

1. **Environment variable** (same for CLI or container):

   ```bash
   # Progress messages: files fetched, agent run, findings count, comments posted
   CODE_REVIEW_LOG_LEVEL=INFO code-review --owner <owner> --repo <repo> --pr <n> [--head-sha <sha>]

   # Verbose (debug)
   CODE_REVIEW_LOG_LEVEL=DEBUG code-review --owner <owner> --repo <repo> --pr <n>
   ```

2. **In `.env`** (remember to `source .env` or export before running; the app does not load `.env` automatically):

   ```
   CODE_REVIEW_LOG_LEVEL=INFO
   ```

3. **In CI/Jenkins**: Add `CODE_REVIEW_LOG_LEVEL=INFO` (or `DEBUG`) to the job’s environment or the container’s env so runs produce progress logs.

**What you see at each level**

| Level    | Typical output |
|----------|-----------------|
| `INFO`   | "Reviewing owner/repo PR N", "Fetched diff, K file(s)", "Running agent...", "Agent returned M finding(s), L to post", "Posted L comment(s)" (or "Dry run: would post L comment(s)"). |
| `DEBUG`  | Same as INFO plus verbose library and internal messages. |
| `WARNING`| Only warnings and errors (default). |
| `ERROR`  | Only errors. |

**Troubleshooting: "Agent returned 0 finding(s)" in file-by-file mode**

When the PR diff is large, the runner splits work by file (file-by-file mode). Some models return fewer or no findings when given a single file at a time. To force **single-shot** mode (full diff in one request, like a small PR or Gitea run), set a higher diff budget so the full diff fits:

```bash
# Use 100% of context window for the diff so file-by-file is not used
LLM_DIFF_BUDGET_RATIO=1.0 code-review --owner <owner> --repo <repo> --pr <n> [--head-sha <sha>]
```

Compare with and without this env var; if you get findings only with `LLM_DIFF_BUDGET_RATIO=1.0`, the issue is file-by-file behaviour. You can leave it at `1.0` for that run or increase `LLM_CONTEXT_WINDOW` so that 25% of it is larger than your typical diff.

### 6.4 Observability

- **Prometheus**: `CODE_REVIEW_METRICS=prometheus` or `CODE_REVIEW_PROMETHEUS=1`; optional deps: `pip install -e ".[observability]"`. Use `code_review.observability.get_prometheus_registry()` to expose `/metrics`.
- **OpenTelemetry**: `CODE_REVIEW_TRACING=otel` or `CODE_REVIEW_OTEL=1`; set `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` for export.


### 6.5 Context Enrichment (`CONTEXT_` prefix)

Opt-in feature for cross-referencing code against linked GitHub Issues, Jira tickets, and
Confluence pages. Disabled by default (`CONTEXT_ENABLED=false`).

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_ENABLED` | `false` | Master switch. Set to `true` to activate. |
| `CONTEXT_GITHUB_ISSUES_ENABLED` | `true` | Fetch linked GitHub Issue content. Uses `SCM_TOKEN`. |
| `CONTEXT_JIRA_ENABLED` | `false` | Fetch linked Jira ticket content. |
| `CONTEXT_JIRA_URL` | — | Jira base URL (e.g. `https://yourcompany.atlassian.net`). |
| `CONTEXT_JIRA_EMAIL` | — | Email for Jira Basic-Auth. |
| `CONTEXT_JIRA_TOKEN` | — | Jira API token. Treated as a secret. |
| `CONTEXT_JIRA_PROJECT_KEYS` | — | Comma-separated project key prefixes (e.g. `PROJ,APP`) to restrict Jira extraction. |
| `CONTEXT_CONFLUENCE_ENABLED` | `false` | Fetch linked Confluence page content. |
| `CONTEXT_CONFLUENCE_URL` | — | Confluence base URL (e.g. `https://yourcompany.atlassian.net/wiki`). |
| `CONTEXT_CONFLUENCE_EMAIL` | — | Email for Confluence Basic-Auth. |
| `CONTEXT_CONFLUENCE_TOKEN` | — | Confluence API token. Treated as a secret. |
| `CONTEXT_MAX_CONTEXT_TOKENS` | `20000` | Token budget for fetched context. Content is truncated when limit is reached. |

See **[CONTEXT-AWARE-REVIEW.md](CONTEXT-AWARE-REVIEW.md)** for setup instructions, reference extraction patterns, and the token budget model.

---

## 7. Extension Points

### 7.1 Adding a New SCM Provider

1. Implement **ProviderInterface** in a new module under `providers/` (e.g. `providers/my_scm.py`). Implement: `get_pr_diff`, `get_pr_diff_for_file`, `get_file_content`, `get_file_lines`, `get_pr_files`, `post_review_comments`, `get_existing_review_comments`, `post_pr_summary_comment`, `get_pr_info`, `capabilities()`.
2. In `providers/__init__.py`, extend **get_provider(name, base_url, token)** to return your provider when `name == "my_scm"` and add it to `__all__`.
3. Extend **SCMConfig.provider** in `config.py` to include `"my_scm"` (e.g. `Literal[..., "my_scm"]`).
4. Add tests under `tests/providers/test_my_scm.py` with mocked HTTP.

### 7.2 Adding or Changing an LLM Backend

- **models.py**: In **get_configured_model()**, handle a new `LLMConfig.provider` value (e.g. another LiteLLM model string or a custom ADK model class). Ensure **get_context_window()** and **get_max_output_tokens()** remain consistent with config.
- **config.py**: Add the new provider to `LLMConfig.provider`’s `Literal` if needed.

### 7.3 Customizing the Agent (instruction / tools)

- **agent/agent.py**: Change **FINDINGS_ONLY_INSTRUCTION** or **BASE_INSTRUCTION**, or pass different `review_standards` from the runner. To add tools, extend **create_findings_only_tools** or **create_gitea_tools** in `agent/tools/gitea_tools.py` and ensure the agent receives them (and the instruction describes when to use them).

### 7.4 Customizing Review Criteria (language / framework)

- **standards/detector.py**: Extend path/config rules or confidence logic.
- **standards/prompts/**: Add or edit per-language fragments and wire them in **get_review_standards()** in `standards/` (e.g. in `prompts/__init__.py` or equivalent).

### 7.5 Comment Format and Fingerprinting

- **formatters/comment.py**: **finding_to_comment_body(f)** controls how each finding becomes comment text.
- **diff/fingerprint.py**: **format_comment_body_with_marker** and **parse_marker_from_comment_body** define the hidden marker format; **build_fingerprint** and **surrounding_content_hash** affect dedup and ignore list.

### 7.6 Programmatic Entry Point

- **run_review(owner, repo, pr_number, head_sha="", *, dry_run=False, print_findings=False)** in `runner.py`: Use this when integrating the agent from another service or script instead of the CLI. Returns `list[FindingV1]` (findings that were or would be posted).

### 7.7 Adding a New Context Source

The context enrichment pipeline (`context/`) is designed for easy extension.

1. **Add a `ReferenceType`** in `context/extractor.py` and extend `extract_references()` with the corresponding regex pattern(s).
2. **Create a fetcher module** `context/my_source.py` that returns a human-readable string for a given identifier.
3. **Wire into `ContextFetcher._fetch_reference()`** in `context/fetcher.py`.
4. **Add credentials fields** to `ContextConfig` in `config.py` and document them in `.env.example`.
5. **Add tests** under `tests/context/test_my_source.py` with mocked HTTP, following the pattern in `tests/context/test_jira.py`.

For large-document sources where per-item content exceeds the token budget, consider a summarisation
pre-pass or a RAG (Retrieval-Augmented Generation) approach inside `ContextFetcher.build_context()`.
See [CONTEXT-AWARE-REVIEW.md §9](CONTEXT-AWARE-REVIEW.md#9-extension) for details.

---

## 8. Orchestrated Deployment (Optional)

For high-concurrency or multi-tenant scenarios, you can run this package as a **stateless worker** behind an external orchestration service.

- **Worker contract**:
  - CLI: `code-review --owner ... --repo ... --pr ... --head-sha ...`
  - Env: `SCM_*` and `LLM_*` as documented above.
  - One invocation of `run_review`:
    - Runs a single review for the given PR/head.
    - Uses hidden markers + an idempotency key to avoid duplicate comments on the same head.
    - Returns `[]` when a run is skipped (e.g. `[skip-review]` label or idempotency key already seen).

- **Idempotency key**:
  - The runner builds an internal key of the form:
    - `{provider}/{owner}/{repo}/pr/{pr_number}/head/{head_sha}/agent/{AGENT_VERSION}/config/{config_hash}`
  - This key is embedded in each posted comment’s hidden marker and used to skip duplicate runs for the same PR/head/config.

- **Orchestration service (sister project)**:
  - Lives in a separate repo and is responsible for:
    - Receiving SCM webhooks / CI callbacks.
    - Debouncing jobs per PR so the **latest head_sha wins**.
    - Ensuring at most one active `run_review` per PR/head at a time (per-PR locking).
    - Starting worker containers/processes that invoke this package via the CLI.
  - See `ORCHESTRATION_PLAN_AGENT.md` (this repo) and `ORCHESTRATION_PLAN_SERVICE.md` (sister project) for a detailed design.

This mode is optional: CI can still call the agent directly without any orchestration layer for low/medium concurrency.

---

## 9. Testing

### 8.1 Test Layout

Tests mirror the source layout and live under `tests/`:

- **cli/test_main.py**: CLI parsing, missing args, env fallback, `fail_on_critical`; mocks `run_review`.
- **test_runner.py**, **runner/test_*.py**: Runner and idempotency, chunking, observability; use a **MockProvider** (or similar) and **patch `google.adk.runners.Runner`** so `run()` yields a single final event with JSON findings (no real LLM).
- **test_providers_gitea.py**, **providers/test_github.py**, **providers/test_gitlab.py**, **providers/test_bitbucket.py**: Provider behavior with **mocked HTTP** (e.g. `@patch("code_review.providers.gitea.httpx.Client")` and set `request.return_value`).
- **tools/test_scm_tools.py**: Agent tools; **MagicMock** for the provider; call tool functions and assert delegation to the provider.
- **integration/test_gitea_agent_integration.py**: Runner + real GiteaProvider with **respx** for Gitea API; **Runner** patched so no real LLM; asserts POST to review endpoint with expected payload.
- **models/test_model_factory.py**: **patch get_llm_config**; assert `get_configured_model()`, `get_context_window()`, `get_max_output_tokens()` for different configs.
- **schemas/test_findings.py**, **standards/test_detector.py**, **standards/test_prompts.py**, **diff/test_*.py**, **formatters/test_comment_format.py**, **observability**: Unit tests for schemas, standards, diff, formatters, observability.
- **e2e/test_docker_gitea_e2e.py**: E2E placeholder; run with `RUN_E2E=1` when Gitea (and env) are available.
- **docker/test_dockerfile.py**: Dockerfile and compose sanity checks.

### 8.2 Mocking Patterns

- **Provider**: Implement a minimal `ProviderInterface` (or use `MagicMock(spec=ProviderInterface)`). In runner tests, **patch `code_review.runner.get_provider`** to return this provider.
- **ADK / LLM**: **Patch `google.adk.runners.Runner`** so the constructed runner’s **run()** returns an iterator yielding one (or more) events where `is_final_response()` is True and `content.parts[].text` is the JSON array of findings. No real Runner or model is invoked.
- **Config**: **Patch `get_scm_config`** and **get_llm_config** (and in runner tests **get_context_window**) so tests do not depend on real env.
- **HTTP (Gitea, etc.)**: Patch `httpx.Client` (or the client used by the provider) and set `.request.return_value` (or `.get`/`.post` if the provider uses them) to a mock response with `.text`, `.json()`, `.headers` as needed.

### 8.3 Running Tests and Tooling

```bash
pip install -e ".[dev]"

# Linting (Ruff). CI runs this as a non-blocking check so lint
# failures show up in GitHub Actions logs but do not fail the build.
ruff check src tests

# Optional local type checking (only if you install mypy yourself)
# mypy src

# Unit and integration tests
pytest

# Exclude E2E (requires RUN_E2E=1 and Docker/Podman):
pytest --ignore=tests/e2e

# Full-stack E2E (auto-managed Gitea + Jenkins stack and hello-world PR)
RUN_E2E=1 pytest -m e2e
```

### 8.4 Pre-commit Hooks (optional)

Pre-commit hooks are **not enabled by default**. The repository ships an empty
`.pre-commit-config.yaml` so that contributors can opt in to local linting and
formatting if they wish.

If you want Ruff to run automatically on `git commit`, install and configure
pre-commit locally:

```bash
pip install pre-commit
pre-commit install
```

Then add your preferred hooks to `.pre-commit-config.yaml`, for example:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.7.0
    hooks:
      - id: ruff
        args: ["--fix"]
      - id: ruff-format
```

---

## 10. References

- **README.md**: Quick start, configuration summary, Docker/CI, observability.
- **.env.example**: Example SCM and LLM env vars.
- **AGENTS.md**: Project summary, where things live, ADK usage, conventions; optional context for the review agent.
- **Google ADK**: [Agent Development Kit](https://google.github.io/adk-docs/) — LlmAgent, Runner, SessionService, function tools.

## 11. Development Testing

For local Gitea + Jenkins testing (including webhook auto-triggers) and for running against remote SCMs during development, see **[Development Testing Guide](DEV_TESTING.md)**.
