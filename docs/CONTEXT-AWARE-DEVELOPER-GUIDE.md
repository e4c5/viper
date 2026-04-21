# Context-Aware Review — Developer Guide

This guide documents the implementation details of context-aware review:

- architecture and flow in the runner
- storage and retrieval internals
- extension points
- test strategy

Use this alongside `docs/CONTEXT-AWARE-USER-GUIDE.md` for operator-facing setup and usage.

---

## Table of Contents

1. [Architecture summary](#1-architecture-summary)
2. [Code map](#2-code-map)
3. [End-to-end flow](#3-end-to-end-flow)
4. [Data model and caching](#4-data-model-and-caching)
5. [RAG path details](#5-rag-path-details)
6. [Failure handling contract](#6-failure-handling-contract)
7. [Configuration model](#7-configuration-model)
8. [Testing strategy](#8-testing-strategy)
9. [Extending with a new context source](#9-extending-with-a-new-context-source)
10. [Operational notes](#10-operational-notes)

---

## 1. Architecture summary

Context-aware review is runner-orchestrated and optional:

1. Runner fetches PR metadata and commit messages.
2. Reference extraction scans title/description/commits.
3. Applicable references are fetched directly, or fetched/loaded through PostgreSQL when a DB URL is configured.
4. Content is distilled directly after byte-budget clamping; with DB/RAG enabled, oversized context uses retrieval + distillation.
5. Distilled brief is appended to prompt in `<context>...</context>`.
6. Agent instruction is conditionally enhanced when context is attached.

The ADK agent remains findings-only; posting/filtering continues to be runner-owned.

---

## 2. Code map

Primary modules:

- `src/code_review/context/extract.py`
  - Conservative regex extraction and deduplication.
- `src/code_review/context/validation.py`
  - Fail-fast validation for enabled sources.
- `src/code_review/context/fetchers.py`
  - Source-specific fetchers + dispatcher + normalization.
- `src/code_review/context/store.py`
  - Optional PostgreSQL/pgvector schema, cache reads/writes, chunk search.
- `src/code_review/context/rag.py`
  - Diff-to-query transform, chunking, embedding helpers.
- `src/code_review/context/pipeline.py`
  - Orchestrates direct fetch/distillation or fetch/cache/budget/retrieval/distillation and returns context brief.
- `src/code_review/runner.py`
  - Integrates context pipeline into main review flow.

Related:

- `src/code_review/config.py` (`ContextAwareReviewConfig`, `CodeReviewAppConfig`)
- `src/code_review/observability.py` (`context_aware` run label)

---

## 3. End-to-end flow

The runner integration point is `_build_prompt_suffix()` in `runner.py`.

High-level sequence:

1. Load context config (`get_context_aware_config()`).
2. If enabled, run `validate_context_aware_sources(ctx_cfg, scm_cfg)`.
3. Load commit messages when needed (`get_pr_commit_messages` via provider).
4. Extract refs from title/description/commits using `extract_context_references(...)`.
5. If refs exist, call `build_context_brief_for_pr(ctx_cfg, scm_cfg, refs, full_diff)`.
6. Build prompt supplement:
   - commit-message block (optional, app-level config)
   - distilled context block (if present)
7. Pass `context_brief_attached` flag through to observability and agent creation.

Inside `build_context_brief_for_pr(...)`:

1. Filter refs by enabled source.
2. If `ctx.db_url` is empty, fetch each applicable reference directly, clamp the combined text to `ctx.max_bytes`, and distill it.
3. If `ctx.db_url` is set, reuse/create `ContextStore` from module-level cache.
4. Open one DB connection and ensure schema.
5. For each applicable ref:
   - resolve source row
   - cache lookup by `(source_id, external_id)`
   - fetch on miss/stale, then upsert
6. Combine resolved docs.
7. If over `max_bytes`, run retrieval path:
   - build semantic query from diff
   - embed query
   - ensure chunk embeddings for docs
   - similarity search (scoped to current docs)
8. Distill selected text to final brief.
9. Return `<context>\n...\n</context>` or `None`.

---

## 4. Data model and caching

Context storage uses three namespaced tables:

- `review_context_sources`
- `review_context_documents`
- `review_context_chunks`

Key properties:

- `sources` unique on `(name, base_url)`.
- `documents` unique on `(source_id, external_id)`.
- `chunks` unique on `(document_id, chunk_index)`.
- Embeddings stored in `vector(<dimensions>)`.
- HNSW index creation is attempted and treated as optional.

Caching behavior:

- Document freshness uses `last_fetched_at` TTL (`_CACHE_TTL_SECONDS` in `store.py`).
- Fresh rows skip remote fetch.
- On document update, existing chunks are replaced for consistency.
- `ContextStore` instances are cached by `(db_url, embedding_dimensions)` to avoid repeated schema DDL per run.

---

## 5. RAG path details

RAG is only available when `CONTEXT_AWARE_REVIEW_DB_URL` is configured. It is only used when combined resolved text exceeds `CONTEXT_MAX_BYTES`.

Implementation details:

- Semantic query is built by `build_semantic_query_from_diff(diff_text)`:
  - first try a lightweight LLM summarization pass
  - fallback to heuristic query from modified file paths
- Documents are chunked with overlap using `chunk_plain_text(...)`.
- Embeddings generated by `embed_texts(...)` and `embed_query_text(...)`.
- Retrieval uses cosine distance and is scoped by `document_id` list from current PR context.
- If retrieved text is still oversized, it is byte-truncated before distillation.

Practical implication: retrieval never pulls chunks from unrelated historical documents when `document_ids` are provided.

---

## 6. Failure handling contract

`validation.py` enforces fatal startup errors for missing required config on enabled sources.

`fetchers.py` behavior:

- 401/403 -> `ContextAwareAuthError` -> fatal (propagated).
- source-level non-auth fatal errors (`ContextAwareFatalError`) -> logged and downgraded to skip that reference.
- transport/runtime errors -> logged and skipped.

Pipeline behavior:

- No applicable refs, no resolved docs, or empty distillation output -> returns `None` (review continues without context).
- Embedding/query failures in RAG path -> raised as `ContextAwareFatalError`.

Runner behavior:

- context-aware fatal setup/runtime errors are surfaced and stop the run.
- normal no-context outcomes continue as standard review.

---

## 7. Configuration model

`ContextAwareReviewConfig` in `config.py` is the source of truth for env aliases/defaults.

Important fields:

- master/DB: `enabled`, `db_url`
- source toggles: `github_issues_enabled`, `gitlab_issues_enabled`, `jira_enabled`, `confluence_enabled`
- credentials/source URLs: `github_*`, `gitlab_*`, `jira_*`, `confluence_*`
- processing: `max_bytes`, `distilled_max_tokens`, `embedding_model`, `embedding_dimensions`
- Jira custom fields: `jira_extra_fields`

Prompt-level companion config:

- `CodeReviewAppConfig.include_commit_messages_in_prompt`

Keep docs aligned with code defaults to avoid operator confusion.

---

## 8. Testing strategy

Context-aware tests live in `tests/context/`.

Current coverage areas:

- `test_context_extract_and_validation.py`
  - extraction patterns, dedup, fenced-code behavior, validation failure cases
- `test_fetchers.py`
  - source fetch happy paths, 404 behavior, 401/403 auth errors, 5xx handling, dispatcher behavior
- `test_rag.py`
  - chunking overlap and edge cases, semantic-query fallback, embedding helper behavior
- `test_pipeline.py`
  - direct vs over-budget retrieval path, cache-hit behavior, fatal propagation, source variants
- `test_distiller.py`
  - distillation-layer behavior and guardrails

Recommended command:

```bash
pytest tests/context -q
```

---

## 9. Extending with a new context source

To add a new source (for example Linear or Notion):

1. Add a new `ReferenceType` and extraction logic in `extract.py`.
2. Add source config fields to `ContextAwareReviewConfig` in `config.py`.
3. Update validation rules in `validation.py`.
4. Implement fetch + normalization in `fetchers.py`.
5. Map source name/base in `_source_name_and_base()` in `pipeline.py`.
6. Add tests:
   - extraction tests
   - fetcher tests (200/404/401+)
   - pipeline integration path tests
7. Update user-facing docs and env var tables.

Design guidance:

- normalize content to compact plain text for distillation.
- avoid recursive graph expansion in first iteration.
- preserve canonical IDs and update timestamps for cache freshness.

---

## 10. Operational notes

- PostgreSQL with `pgvector` is optional; configure `CONTEXT_AWARE_REVIEW_DB_URL` when cache/RAG is needed.
- Embedding dimension mismatches will fail chunk persistence.
- Distillation consumes model tokens in all modes; semantic-query generation and embeddings add cost only in the RAG path.
- Observability includes a `context_aware` Prometheus label so adoption can be tracked.
- For very noisy context sources, tune `CONTEXT_MAX_BYTES` and `CONTEXT_DISTILLED_MAX_TOKENS`.
