# Context-Aware Code Review

The code review agent can optionally **enrich its analysis with the content of linked
GitHub Issues, Jira tickets, and Confluence pages**. When enabled, the agent
uses that context to:

- Understand the requirements and acceptance criteria behind the change.
- Flag mismatches between what was stated in the ticket/issue and what the code actually does.
- Identify missing implementation steps or incorrect assumptions.

This feature is **opt-in and disabled by default**. It does not change any existing
behaviour unless `CONTEXT_AWARE_REVIEW_ENABLED=true` is set.

---

## Table of Contents

1. [How it works](#1-how-it-works)
2. [Enabling context enrichment](#2-enabling-context-enrichment)
3. [Reference extraction — what patterns are recognised](#3-reference-extraction)
4. [GitHub Issues](#4-github-issues)
5. [Jira](#5-jira)
6. [Confluence](#6-confluence)
7. [Context budget and Distillation](#7-context-budget-and-distillation)
8. [RAG Implementation Details](#8-rag-implementation-details)
9. [Architecture overview](#9-architecture-overview)
10. [Extension — adding a new context source](#10-extension--adding-a-new-context-source)
11. [Implementation roadmap](#11-implementation-roadmap)

---

## 1. How it works

1. **Extraction** — The **runner** scans the PR title, PR description, and all commit messages
   included in the PR for known reference patterns (GitHub issue numbers, Jira ticket keys,
   Confluence page URLs).
2. **Lookup & Fetch** — For each unique reference, the **runner** checks the context database.
   If the content is missing or stale, it is fetched via the relevant API (GitHub, Jira, or
   Confluence) and cached.
3. **Budget Check** — The runner evaluates the total size of the relevant context.
4. **Processing (Direct vs. Retrieval)**:
   - **Under the configured budget**: The full relevant content is passed to the **Distiller**.
   - **Over the configured budget**: The runner retrieves only the most relevant chunks and
     passes those chunks to the **Distiller**.
5. **Distillation** — The Distiller (a specialized LLM pass) summarizes the provided context
   into a concise brief.
6. **Prompt inclusion** — The distilled brief is wrapped in `<context>…</context>` tags
   and added to the review prompt by the runner.
   Separately, PR commit messages can also be included alongside the diff to help the model
   understand the author's stated intent. This is independent of context-aware review,
   controlled by its own configuration flag, and enabled by default.
7. **LLM instruction** — When context is present, the agent instruction is extended with
   guidance to cross-check code against the stated requirements and flag mismatches.

If no references are found, the review proceeds without any additional context.
If context enrichment is enabled, configuration and authentication failures for enabled
sources are treated as fatal and stop the review.

---

## 2. Enabling context enrichment

Set `CONTEXT_AWARE_REVIEW_ENABLED=true` and configure credentials for any source you want
to enable.

> [!IMPORTANT]
> If `CONTEXT_AWARE_REVIEW_ENABLED=true`, the runner should fail fast when an enabled
> source is misconfigured or cannot authenticate. In particular:
> - Missing required credentials for an enabled source are fatal.
> - Authentication or authorization failure (HTTP 401/403) for an enabled source is fatal.
> - Transient or server-side errors (HTTP 5xx, network timeouts) for a specific reference
>   are logged as warnings and that reference is skipped; the review continues with whatever
>   context was successfully fetched. This avoids aborting an entire review due to a single
>   temporarily unavailable external system.
> - If no references are found, the review continues normally without added context.

### Database Schema

All fetched context is preserved in a normalized database with `pgvector` to avoid redundant API calls and enable efficient RAG even for large documents. See [PGVECTOR-SCHEMA.md](PGVECTOR-SCHEMA.md) for details.

### Minimal — GitHub Issues only (no extra credentials)

When `SCM_PROVIDER=github`, the existing `SCM_TOKEN` is reused automatically; no
additional credentials are needed.

```bash
CONTEXT_AWARE_REVIEW_ENABLED=true
# CONTEXT_GITHUB_ISSUES_ENABLED=true  # already the default
```

### GitHub Issues + Jira

```bash
CONTEXT_AWARE_REVIEW_ENABLED=true

CONTEXT_JIRA_ENABLED=true
CONTEXT_JIRA_URL=https://yourcompany.atlassian.net
CONTEXT_JIRA_EMAIL=you@yourcompany.com
CONTEXT_JIRA_TOKEN=your_jira_api_token
```

### All `CONTEXT_*` environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_AWARE_REVIEW_ENABLED` | `false` | Master switch. Set to `true` to activate. |
| `CONTEXT_GITHUB_ISSUES_ENABLED` | `true` | Fetch linked GitHub Issue content when enabled. Uses `SCM_TOKEN`. |
| `CONTEXT_GITLAB_ISSUES_ENABLED` | `false` | Fetch linked GitLab Issue content when enabled. Uses `SCM_TOKEN` when `SCM_PROVIDER=gitlab`, otherwise requires `CONTEXT_GITLAB_TOKEN`. |
| `CONTEXT_JIRA_ENABLED` | `false` | Fetch linked Jira ticket content. Requires `CONTEXT_JIRA_URL`, `CONTEXT_JIRA_EMAIL`, etc. |
| `CONTEXT_JIRA_URL` | — | Jira base URL. |
| `CONTEXT_JIRA_EMAIL` | — | Jira account email used for API access. |
| `CONTEXT_JIRA_TOKEN` | — | Jira API token (treated as a secret). |
| `CONTEXT_JIRA_EXTRA_FIELDS` | — | Comma-separated list of additional Jira field names to fetch (e.g. `customfield_10016,customfield_10014`). Values are included in the distillation context. Useful for acceptance-criteria or other custom fields. |
| `CONTEXT_CONFLUENCE_ENABLED` | `false` | Fetch linked Confluence page content. |
| `CONTEXT_CONFLUENCE_URL` | — | Confluence base URL. |
| `CONTEXT_CONFLUENCE_EMAIL` | — | Confluence account email used for API access. |
| `CONTEXT_CONFLUENCE_TOKEN` | — | Confluence API token (treated as a secret). |
| `CONTEXT_MAX_BYTES` | `20000` | Threshold in bytes. Above this, RAG is used before distillation. |
| `CONTEXT_DISTILLED_MAX_TOKENS` | `4000` | Maximum token budget for the distilled brief added to the review prompt. |

### Additional review-prompt configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CODE_REVIEW_INCLUDE_COMMIT_MESSAGES_IN_PROMPT` | `true` | Include PR commit messages alongside the diff in the review prompt for any review run, even when context-aware review is disabled. Set to `false` to disable. |

---

## 3. Reference extraction

References are extracted from the PR title, description, and all commit messages.
Extraction should be conservative to reduce false positives.

### GitHub Issues

Recognises:

- `#NNN` only when the current review is running against GitHub and the reference is assumed
  to belong to the same repository
- `GH-NNN` when a team explicitly uses that convention
- Full URLs like `https://github.com/org/repo/issues/42`

### Jira

Recognises keys like `PROJ-123` or browse URLs. Extraction should avoid matching strings
inside code blocks, stack traces, or generated text where possible.

### Confluence

Recognises space-based or display-based URLs.

---

## 4. GitHub Issues

For GitHub-hosted repositories, linked issues are the lightest-weight context source because
the existing `SCM_TOKEN` can often be reused. The fetched content should focus on:

- Title
- Body
- Labels
- State
- Key comments only if explicitly configured later

Issue comments should not be part of the first implementation unless they are clearly needed,
because they can expand context volume quickly and introduce a lot of noise.

## 5. Jira

Jira tickets are usually the highest-value structured requirements source. The first
implementation should fetch a compact representation of:

- Summary
- Description
- Issue type
- Status
- Acceptance criteria or custom fields when available
- Linked issue keys only as references, not recursive full fetches

Do not recursively pull an entire linked issue graph in the initial version.

## 6. Confluence

Confluence pages can be large and noisy, so they should be normalized before storage:

- Strip navigation chrome and page metadata that is not useful for review
- Preserve headings and section structure
- Convert rich text to a compact plain-text or markdown-like representation
- Record page version and update time so staleness can be detected

Confluence is the strongest candidate for retrieval-backed processing because page size can
grow far beyond what should be sent directly to the model.

---

## 7. Context budget and Distillation

The agent prioritizes efficiency and token conservation:

- **Token budgets first**: use token-based limits for prompt construction, with byte size as a
  secondary approximation for storage and rough thresholds.
- **Distiller**: Regardless of size, context is summarized by a distiller LLM. This ensures the
  main review agent receives only the most relevant requirements, reducing noise and token costs.
- **Retrieval for oversized context**: Only activate retrieval when the total context exceeds the
  configured budget. This keeps the default path simple and avoids embedding or retrieval work
  when the documents are already small enough.

## 8. RAG Implementation Details

When the total fetched context exceeds the configured budget, the runner can use a
retrieval-backed pipeline using the [PGVector schema](PGVECTOR-SCHEMA.md) to manage the volume of data effectively.

### 8.1 PR Diff as a Query

To retrieve the most relevant segments from a retrieval-capable context store, the runner
transforms the raw PR diff into a **Semantic Search Query**:

1. **Diff Summarization**: A lightweight LLM pass (pre-step) analyzes the diff to identify the core "what" and "why" of the change (e.g., "Updating the JWT validation logic to support multi-issuer keys in `auth_middleware.py`").
2. **Entity Extraction**: Key function names, class names, and modified file paths are extracted
   to ensure the search is grounded in the modified components.
3. **Query Construction**: The summarized intent and extracted entities are combined into a dense vector query. This "search intent" is far more effective for similarity search than a raw unified diff format.

### 8.2 Retrieval & Distillation

- **Similarity Search**: The runner uses the semantic query to pull the most relevant segments
  from the retrieval backend.
- **Distillation**: The retrieved segments (and the original PR metadata) are passed to the
  **Distiller LLM**, which synthesizes them into the final summary used by the review agent.

This two-step process (Semantic Query -> RAG -> Distiller) ensures that the context is both relevant to the code changes and concise enough for an efficient review.

---

## 9. Architecture overview

```text
runner.py
│
├── fetch PR metadata, files, diff, and commit messages
├── extract_context_references()
│     ├── parse title / description / commit messages
│     └── dedupe canonical references
├── optionally include commit messages in the review prompt
│
├── resolve_context()
│     ├── Check database (cache hit?)
│     └── fetch_content() on cache miss or stale entry
│
├── budget_context()
│     ├── under budget -> direct distillation
│     └── over budget -> retrieval + distillation
│
├── build distilled_context_brief
│
└── create_review_agent(review_standards, context_brief)
```

The runner remains the orchestrator. The agent still receives only prompt-ready review context
and remains responsible for findings generation only.

---

## 10. Extension — adding a new context source

The architecture allows adding new sources (e.g., Notion, Linear) by:
1. Adding a new `ReferenceType` in the extractor.
2. Implementing a fetcher for the new source.
3. Defining canonical IDs, freshness rules, and content normalization for that source.
4. Reusing the same database schema and distillation pipeline.
---

## 11. Implementation roadmap

The following items are planned for the implementation of the context-aware review feature:

### 11.1 Phase 1 — Infrastructure & Discovery
- [x] **Configuration**: Add `CONTEXT_AWARE_REVIEW_DB_URL` and `CONTEXT_*` settings to `config.py`, plus `CODE_REVIEW_INCLUDE_COMMIT_MESSAGES_IN_PROMPT` with a default of `true`.
- [x] **Validation**: Validate source configuration per source and fail fast for enabled sources with missing required credentials.
- [x] **Extraction**: Implement conservative reference extraction for PR titles, descriptions, and commit messages.
- [x] **Provider contract**: Extend `ProviderInterface` with PR commit-message retrieval and implement it across all built-in providers.
- [x] **Abstractions**: Introduce `ReferenceExtractor`, `ContextFetcher`, and `ContextDistiller` interfaces.

### 11.2 Phase 2 — Direct Fetch + Distillation
- [x] **Fetchers**: Implement individual fetchers for GitHub (SCM reuse), Jira, and Confluence.
- [x] **Normalization**: Normalize each source into a compact text representation plus metadata such as canonical ID and `updated_at`.
- [x] **Database**: Implement schema for `sources`, `documents`, and `chunks` in PostgreSQL.
- [x] **Store**: Implement cache-backed database logic to minimize redundant API calls.
- [x] **Distiller**: Implement a summary LLM pass that turns fetched context into a concise review brief.

### 11.3 Phase 3 — Runner Integration
- [x] **Runner flow**: Insert context enrichment into the existing runner flow after PR metadata/diff retrieval and before agent creation.
- [x] **Prompting**: Pass the distilled context into both single-shot and file-by-file review modes.
- [x] **Commit-message prompting**: Add a config-controlled path to include PR commit messages in the LLM prompt alongside the diff for all review runs, independent of whether context-aware review is enabled. Default this to on, with an option to disable.
- [x] **Instruction update**: Extend the agent instruction only when context is present, without changing findings-only behaviour.
- [x] **Failure handling**: Stop the review on configuration or authentication failures for enabled sources, while allowing normal reviews to continue when no references are found.

### 11.4 Phase 4 — Retrieval For Oversized Context
- [x] **Retrieval backend**: Implement chunking and embedding logic to populate `chunks` table.
- [x] **Semantic Search**: Implement the diff-summarization pre-pass for retrieval query construction.
- [x] **Chunk retrieval**: Retrieve the most relevant chunks for oversized documents before distillation.

### 11.5 Phase 5 — Verification & Observability
- [x] **Verification**: Unit tests for extraction, validation, distillation/RAG helpers, and `get_pr_commit_messages` (GitHub). Live PostgreSQL + end-to-end context runs are left to environments with `CONTEXT_*` set.
- [x] **Provider coverage**: `get_pr_commit_messages` is implemented for all built-in providers; GitHub has a dedicated unit test (others follow the same httpx client pattern).
- [x] **Mode coverage**: Existing runner tests exercise file-by-file and single-shot modes; both append the same optional prompt suffix when context/commit blocks are present.
- [x] **Metrics and logs**: Structured `logger.info` / `logger.debug` for reference counts, cache hits, distillation, and over-budget retrieval; extend `observability.py` with Prometheus labels if you need dashboards.
