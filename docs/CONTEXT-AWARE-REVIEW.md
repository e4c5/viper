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
10. [Extension — adding a new context source](#10-extension)
11. [Implementation roadmap](#11-implementation-roadmap)

---

## 1. How it works

1. **Extraction** — The runner scans the PR title, PR description, and all commit messages
   included in the PR for known reference patterns (GitHub issue numbers, Jira ticket keys,
   Confluence page URLs).
2. **Lookup & Fetch** — For each unique reference, the agent first checks the **persistent vector store**. If the content is missing or stale, it is fetched via the relevant API (GitHub, Jira, or Confluence) and indexed.
3. **Threshold Check** — The total size of the relevant context associated with the PR is evaluated.
4. **Processing (RAG vs. Direct)**:
   - **Under 20,000 bytes**: The full relevant content is pulled from the vector store and passed to the **Distiller**.
   - **Over 20,000 bytes**: Relevant chunks are retrieved via **RAG** from the vector store based on the PR diff and then passed to the **Distiller**.
5. **Distillation** — The Distiller (a specialized LLM pass) summarizes the provided context into a concise brief.
6. **Prompt inclusion** — The distilled brief is wrapped in `<context>…</context>` tags
   and appended to the user message sent to the LLM.
7. **LLM instruction** — When context is present, the agent instruction is extended with
   guidance to cross-check code against the stated requirements and flag mismatches.

If no references are found, the review proceeds without any context.

---

## 2. Enabling context enrichment

Set `CONTEXT_AWARE_REVIEW_ENABLED=true` and configure credentials for at least one source.

> [!IMPORTANT]
> If `CONTEXT_AWARE_REVIEW_ENABLED=true` but no valid secrets are provided for any enabled source (Jira, GitHub, or Confluence), the agent will throw an exception and stop the review.

### Persistent Vector Store

All fetched context is preserved in a vector store (e.g., PGVector) to avoid redundant API calls and enable efficient RAG even for large documents.

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
| `CONTEXT_JIRA_ENABLED` | `false` | Fetch linked Jira ticket content. Requires `CONTEXT_JIRA_URL`, `CONTEXT_JIRA_EMAIL`, etc. |
| `CONTEXT_JIRA_TOKEN` | — | Jira API token (treated as a secret). |
| `CONTEXT_CONFLUENCE_ENABLED` | `false` | Fetch linked Confluence page content. |
| `CONTEXT_CONFLUENCE_TOKEN` | — | Confluence API token (treated as a secret). |
| `CONTEXT_MAX_BYTES` | `20000` | Threshold in bytes. Above this, RAG is used before distillation. |

---

## 3. Reference extraction

References are extracted from the PR title, description, and all commit messages.

### GitHub Issues

Recognises `#NNN`, `GH-NNN`, or full URLs like `https://github.com/org/repo/issues/42`.

### Jira

Recognises keys like `PROJ-123` or browse URLs.

### Confluence

Recognises space-based or display-based URLs.

---

## 7. Context budget and Distillation

The agent prioritizes efficiency and token conservation:

- **Threshold: 20,000 bytes**: This is the "sweet spot" where many documents can be processed directly.
- **Distiller**: Regardless of size, context is summarized by a distiller LLM. This ensures the main review agent receives only the most relevant requirements, reducing noise and token costs.
- **RAG (Retrieval Augmented Generation)**: Only activated when the total context size exceeds 20,000 bytes. It ensures that even with massive design documents, the most relevant chunks are selected for distillation.

## 8. RAG Implementation Details

When the total fetched context exceeds **20,000 bytes**, the agent uses a **Retrieval-Augmented Generation (RAG)** pipeline to manage the volume of data effectively.

### 8.1 PR Diff as a Query

To retrieve the most relevant segments from the vector store, the agent transforms the raw PR diff into a **Semantic Search Query**:

1. **Diff Summarization**: A lightweight LLM pass (pre-step) analyzes the diff to identify the core "what" and "why" of the change (e.g., "Updating the JWT validation logic to support multi-issuer keys in `auth_middleware.py`").
2. **Entity Extraction**: Key function names, class names, and modified file paths are extracted to ensure the search is grounded in the modified components.
3. **Query Construction**: The summarized intent and extracted entities are combined into a dense vector query. This "search intent" is far more effective for similarity search than a raw unified diff format.

### 8.2 Retrieval & Distillation

- **Similarity Search**: The agent uses the semantic query to pull the most relevant segments from the **PGVector** store.
- **Distillation**: The retrieved segments (and the original PR metadata) are passed to the **Distiller LLM**, which synthesizes them into the final summary used by the review agent.

This two-step process (Semantic Query -> RAG -> Distiller) ensures that the context is both relevant to the code changes and concise enough for an efficient review.

---

## 9. Architecture overview

```
runner.py
│
├── _fetch_pr_context()
│     ├── extract_references()
│     ├── Check Vector Store (Cache Hit?)
│     └── fetch_content() (on Cache Miss) -> Save to Vector Store
│
├── Size Evaluation
│     ├── < 20KB: Pull full content from Vector Store -> Distiller
│     └── > 20KB: RAG (retrieve chunks from Vector Store) -> Distiller
│
├── Distiller (LLM Summarization)
│     └── distilled_context_brief
│
└── create_review_agent(pr_context=distilled_context_brief)
```

---

## 10. Extension — adding a new context source

The architecture allows adding new sources (e.g., Notion, Linear) by:
1. Adding a new `ReferenceType` in the extractor.
2. Implementing a fetcher for the new source.
---

## 11. Implementation roadmap

The follow items are planned for the initial implementation of the context-aware review feature:

### 11.1 Phase 1 — Infrastructure & Discovery
- [ ] **Configuration**: Add `CONTEXT_AWARE_REVIEW_ENABLED` and credentials for Jira, GitHub, and Confluence to `config.py`.
- [ ] **Validation**: Implement secret validation that throws an exception if the feature is enabled but no secrets are provided.
- [ ] **Extraction**: Implement `extract_references()` to scan PR titles, descriptions, and commit messages.

### 11.2 Phase 2 — Persistence & Fetching
- [ ] **Vector Store**: Set up **PGVector** integration for persistent storage and retrieval.
- [ ] **Fetchers**: Implement individual fetchers for GitHub (SCM reuse), Jira (REST v2), and Confluence (REST).
- [ ] **Content Management**: Implement the lookup-and-fetch logic to minimize redundant API calls.

### 11.3 Phase 3 — RAG & Distillation
- [ ] **Semantic Search**: Implement the diff-summarization pre-pass for vector query construction.
- [ ] **RAG Pipeline**: Implement retrieval for documents > 20,000 bytes.
- [ ] **Distiller**: Implement the summary LLM pass to provide concise, high-density context to the review agent.

### 11.4 Phase 4 — Integration & Testing
- [ ] **Runner Integration**: Wire the distilled context into `Runner._fetch_pr_context()`.
- [ ] **Verification**: Add comprehensive unit and integration tests across all components.
