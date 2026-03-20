# Context-Aware Review — Post-Implementation Improvement Checklist

Identified during validation of the context-aware code review implementation against
`CONTEXT-AWARE-REVIEW.md`. Items are ordered by priority.

---

## High Priority

- [x] **`search_chunks` not scoped to the current PR's documents**
  `store.py:search_chunks` runs without a `WHERE document_id = ANY(...)` filter,
  so in a shared database it will pull semantically similar chunks from *any* historical
  document, not just those fetched for the current PR. The `doc_ids_for_rag` list is
  already available at the call site in `pipeline.py` and should be passed through as a
  filter argument.

---

## Medium Priority

- [x] **Missing unit tests for `rag.py`**
  `chunk_plain_text`, `build_semantic_query_from_diff`, and the embedding helpers have
  no tests. Add a `tests/context/test_rag.py` covering at least: chunking with overlap,
  edge cases (empty text, text shorter than chunk size), and the heuristic fallback path
  in `build_semantic_query_from_diff`.

- [x] **Missing unit tests for `fetchers.py`**
  `fetch_jira_issue`, `fetch_confluence_page`, and `fetch_gitlab_issue` are untested.
  Add mocked-HTTP tests (following the pattern in `tests/providers/test_github.py`) for
  the happy path, 404 handling, and 401/403 raising `ContextAwareAuthError`.

- [x] **Missing unit tests for `pipeline.py`**
  No integration-level test exercises the full
  extract → fetch → cache → budget-decision → distill flow with mocked store and
  fetchers. Add at least one test for the under-budget direct path and one for the
  over-budget RAG path.

- [x] **Confluence display-name URLs not extracted** (added `viewpage.action?pageId=` pattern; pure display-name title URLs require a separate API lookup and remain out of scope)
  `extract.py:_CONFLUENCE_PAGE_URL` only matches numeric page IDs (`/pages/(\d+)`).
  URLs of the form `https://wiki.example.com/display/SPACE/Page+Title` are silently
  ignored, even though the spec says "Recognises space-based or display-based URLs."
  Extend the regex (or add a second pattern) and add a corresponding unit test.

---

## Low Priority

- [ ] **No Prometheus labels for context enrichment**
  `context_brief_attached` is passed to `create_review_agent` but never reaches
  `observability.finish_run`. Add a `context_aware` label to the existing Prometheus
  run counter so operators can track context adoption and distillation failures on
  dashboards.

- [ ] **Jira acceptance-criteria / custom fields not fetched**
  `fetch_jira_issue` requests only `summary,description,issuetype,status,updated`.
  Add an optional `CONTEXT_JIRA_EXTRA_FIELDS` config variable (comma-separated field
  names) that is appended to the `fields` query parameter, allowing teams to pull
  acceptance-criteria or other custom fields relevant to their Jira setup.

- [ ] **`CONTEXT_GITLAB_ISSUES_ENABLED` missing from env var table**
  The environment variable table in `CONTEXT-AWARE-REVIEW.md` (section 2) lists
  `CONTEXT_GITHUB_ISSUES_ENABLED` but omits `CONTEXT_GITLAB_ISSUES_ENABLED`, even
  though it is fully implemented. Add the missing row.

- [ ] **Silent skip on non-auth fatal fetch errors**
  In `fetch_reference`, a `ContextAwareFatalError` from a non-auth HTTP error (e.g.
  Jira returning 500) is downgraded to a warning and the reference is silently skipped.
  The plan says enabled-source failures should be fatal. Either enforce this, or
  document clearly that only 401/403 are fatal while server errors are skipped, and
  update `CONTEXT-AWARE-REVIEW.md` accordingly.

- [ ] **Two DB connections opened per `build_context_brief_for_pr` call**
  `_load_context_documents` opens and closes one connection; `_build_retrieved_context_text`
  opens another. Refactor `pipeline.py` to open a single connection at the top of
  `build_context_brief_for_pr` and pass it through both helpers.

- [ ] **`ContextStore._schema_ok` not persisted across reviews**
  A new `ContextStore` is instantiated on every call, so `_schema_ok` is always `False`
  and `ensure_schema` (with its `CREATE EXTENSION / CREATE TABLE IF NOT EXISTS` DDL)
  runs on every review. Cache the store instance or use a module-level flag to skip
  redundant schema checks after the first successful run.
