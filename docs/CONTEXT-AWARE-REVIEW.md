# Context-Aware Code Review

The code review agent can optionally **enrich its analysis with the content of linked
GitHub Issues, Jira tickets, and Confluence pages**. When enabled, the agent
uses that context to:

- Understand the requirements and acceptance criteria behind the change.
- Flag mismatches between what was stated in the ticket/issue and what the code actually does.
- Identify missing implementation steps or incorrect assumptions.

This feature is **opt-in and disabled by default**. It does not change any existing
behaviour unless `CONTEXT_ENABLED=true` is set.

---

## Table of Contents

1. [How it works](#1-how-it-works)
2. [Enabling context enrichment](#2-enabling-context-enrichment)
3. [Reference extraction — what patterns are recognised](#3-reference-extraction)
4. [GitHub Issues](#4-github-issues)
5. [Jira](#5-jira)
6. [Confluence](#6-confluence)
7. [Token budget](#7-token-budget)
8. [Architecture overview](#8-architecture-overview)
9. [Extension — adding a new context source](#9-extension)
10. [Design considerations: RAG vs. eager fetch](#10-design-considerations-rag-vs-eager-fetch)

---

## 1. How it works

1. **Extraction** — The runner scans the PR title, PR description, and all commit messages
   included in the PR for known reference patterns (GitHub issue numbers, Jira ticket keys,
   Confluence page URLs).
2. **Fetching** — For each unique reference, the corresponding content is fetched via the
   relevant API (GitHub REST, Jira REST v2, Confluence REST).
3. **Budget enforcement** — Items are accumulated until the configurable token budget
   (`CONTEXT_MAX_CONTEXT_TOKENS`, default 20 000) is reached. Content that would exceed the
   remaining budget is truncated.
4. **Prompt injection** — The assembled context is wrapped in `<context>…</context>` tags
   and appended to the user message sent to the LLM in both single-shot and file-by-file mode.
5. **LLM instruction** — When context is present, the agent instruction is extended with
   guidance to cross-check code against the stated requirements and flag mismatches.

If no references are found, or if all fetches fail, the review proceeds without
any context (identical to the non-enriched mode).

---

## 2. Enabling context enrichment

Set `CONTEXT_ENABLED=true` and configure credentials for the sources you want to use.

### Minimal — GitHub Issues only (no extra credentials)

When `SCM_PROVIDER=github`, the existing `SCM_TOKEN` is reused automatically; no
additional credentials are needed.

```bash
CONTEXT_ENABLED=true
# CONTEXT_GITHUB_ISSUES_ENABLED=true  # already the default
```

### GitHub Issues + Jira

```bash
CONTEXT_ENABLED=true

CONTEXT_JIRA_ENABLED=true
CONTEXT_JIRA_URL=https://yourcompany.atlassian.net
CONTEXT_JIRA_EMAIL=you@yourcompany.com
CONTEXT_JIRA_TOKEN=your_jira_api_token

# Optional: restrict extraction to specific project key prefixes
# CONTEXT_JIRA_PROJECT_KEYS=PROJ,MYAPP
```

### GitHub Issues + Jira + Confluence

```bash
CONTEXT_ENABLED=true

CONTEXT_JIRA_ENABLED=true
CONTEXT_JIRA_URL=https://yourcompany.atlassian.net
CONTEXT_JIRA_EMAIL=you@yourcompany.com
CONTEXT_JIRA_TOKEN=your_jira_api_token

CONTEXT_CONFLUENCE_ENABLED=true
CONTEXT_CONFLUENCE_URL=https://yourcompany.atlassian.net/wiki
CONTEXT_CONFLUENCE_EMAIL=you@yourcompany.com
CONTEXT_CONFLUENCE_TOKEN=your_confluence_api_token
```

### All `CONTEXT_*` environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_ENABLED` | `false` | Master switch. Set to `true` to activate. |
| `CONTEXT_GITHUB_ISSUES_ENABLED` | `true` | Fetch linked GitHub Issue content when `CONTEXT_ENABLED=true`. Uses `SCM_TOKEN` — no extra credentials required. |
| `CONTEXT_JIRA_ENABLED` | `false` | Fetch linked Jira ticket content. Requires `CONTEXT_JIRA_URL`, `CONTEXT_JIRA_EMAIL`, `CONTEXT_JIRA_TOKEN`. |
| `CONTEXT_JIRA_URL` | — | Jira base URL, e.g. `https://yourcompany.atlassian.net` (Cloud) or `https://jira.internal` (Server/DC). |
| `CONTEXT_JIRA_EMAIL` | — | Email address used for Jira Basic-Auth (Cloud) or username (Server/DC). |
| `CONTEXT_JIRA_TOKEN` | — | Jira API token (Cloud) or password (Server/DC). Treated as a secret. |
| `CONTEXT_JIRA_PROJECT_KEYS` | — | Comma-separated Jira project key prefixes to restrict extraction (e.g. `PROJ,MYAPP`). When unset, all `WORD-NNN` patterns are considered Jira keys. |
| `CONTEXT_CONFLUENCE_ENABLED` | `false` | Fetch linked Confluence page content. Requires `CONTEXT_CONFLUENCE_URL`, `CONTEXT_CONFLUENCE_EMAIL`, `CONTEXT_CONFLUENCE_TOKEN`. |
| `CONTEXT_CONFLUENCE_URL` | — | Confluence base URL, e.g. `https://yourcompany.atlassian.net/wiki`. A trailing `/wiki` suffix is handled automatically. |
| `CONTEXT_CONFLUENCE_EMAIL` | — | Email/username for Confluence Basic-Auth. |
| `CONTEXT_CONFLUENCE_TOKEN` | — | Confluence API token or password. Treated as a secret. |
| `CONTEXT_MAX_CONTEXT_TOKENS` | `20000` | Maximum tokens to spend on fetched context. Content is truncated when this limit is reached. |

---

## 3. Reference extraction

References are extracted from three text sources (combined into a single pass):

- PR **title**
- PR **description** (body)
- Each **commit message** in the PR

### GitHub Issues

| Pattern | Example | Notes |
|---------|---------|-------|
| `#NNN` shorthand | `Fixes #42` | Only recognised when `SCM_PROVIDER=github` and the current `owner`/`repo` are known. |
| `GH-NNN` shorthand | `GH-42` | Same restriction as above. |
| Full GitHub URL | `https://github.com/org/repo/issues/42` | Always recognised; `owner`/`repo` extracted from the URL. |

### Jira

| Pattern | Example | Notes |
|---------|---------|-------|
| Bare key | `PROJ-123` or `MYAPP-42` | Must start with an uppercase letter; `proj-123` (all-lowercase) is not matched. |
| Browse URL | `https://company.atlassian.net/browse/PROJ-123` | Always recognised. |

Use `CONTEXT_JIRA_PROJECT_KEYS` to avoid false positives (e.g. variable names like `HTTP-1`).

### Confluence

| Pattern | Example |
|---------|---------|
| Spaces URL with page ID | `https://company.atlassian.net/wiki/spaces/ENG/pages/12345678/My+Page` |
| Display URL | `https://company.atlassian.net/wiki/display/ENG/My+Page` |

Duplicate references (same issue/key/page appearing multiple times across title, description,
and commits) are deduplicated before any API call is made.

---

## 4. GitHub Issues

- **Credentials**: reuses the existing `SCM_TOKEN` when `SCM_PROVIDER=github`. No extra
  credentials are needed for issues in the same repository.
- **Content fetched**: issue title, state (open/closed), labels, body, and up to 10 comments.
- **Errors**: any HTTP or network error is silently logged at DEBUG level; the review
  continues without that issue's context.

---

## 5. Jira

- **API**: Jira REST API v2 (`/rest/api/2/issue/{key}`). Works with both Jira Cloud and
  Jira Server / Data Center.
- **Authentication**: HTTP Basic Auth (email + API token for Cloud; username + password for
  Server/DC).
- **Content fetched**: summary, description (plain text or Atlassian Document Format),
  issue type, status, priority, labels, and up to 10 comments.
- **Project key filter**: set `CONTEXT_JIRA_PROJECT_KEYS=PROJ,APP` to restrict extraction
  to specific projects. This avoids treating version numbers like `HTTP-2` or internal codes
  like `V8-0` as Jira tickets.
- **Errors**: silently logged at DEBUG level; the review continues without that ticket's context.

---

## 6. Confluence

- **API**: Confluence REST API (`/wiki/rest/api/content/{page_id}` or
  `/wiki/rest/api/content?spaceKey=…&title=…`). Works with both Confluence Cloud and
  Confluence Server / Data Center.
- **Authentication**: HTTP Basic Auth (email + API token for Cloud; username + password for
  Server/DC). Must match the `CONTEXT_CONFLUENCE_EMAIL` / `CONTEXT_CONFLUENCE_TOKEN` settings.
- **Content fetched**: page title, space name, and page body (storage format, converted from
  HTML to plain text). Body is truncated to `4 000` characters per page before the global
  token budget is applied.
- **URL handling**: the fetcher attempts to extract a numeric page ID from URLs first
  (`.../pages/12345678/…` or `?pageId=12345678`). When no ID is present, it falls back to a
  title-based search using the space key and title extracted from the URL.
- **Errors**: silently logged at DEBUG level; the review continues without that page's context.

---

## 7. Token budget

The total amount of fetched context text is capped by `CONTEXT_MAX_CONTEXT_TOKENS`
(default **20 000 tokens**; 1 token ≈ 4 characters).

Items are added in the order they appear in the combined title + description + commits text.
When adding the next item would exceed the remaining budget, the item is **truncated** to fit
(not skipped entirely), and no further items are added.

The context budget is separate from the diff budget (`LLM_DIFF_BUDGET_RATIO`). Both are
injected into the user message; ensure that `LLM_CONTEXT_WINDOW` is large enough to
accommodate both the diff and the context comfortably.

**Example sizing:**

| LLM_CONTEXT_WINDOW | DIFF_BUDGET_RATIO | Diff budget | Remaining for context + prompt |
|--------------------|-------------------|-------------|-------------------------------|
| 128 000 | 0.25 | 32 000 tokens | ~96 000 tokens |
| 128 000 | 0.25 | 32 000 tokens | Context capped at 20 000 by default |

If you find the context is being truncated too aggressively, increase
`CONTEXT_MAX_CONTEXT_TOKENS` (e.g. `CONTEXT_MAX_CONTEXT_TOKENS=40000`) while ensuring it
still leaves room for the diff and system prompt.

> **When does truncation become a real problem?**  
> For typical Jira tickets and GitHub issues (a few hundred to ~2 000 tokens each) the
> default 20 000-token budget is generous and truncation rarely triggers. It becomes a
> concern when PRs reference Confluence pages with tens of thousands of words, or when a
> single PR references many issues. See
> [§10 Design considerations: RAG vs. eager fetch](#10-design-considerations-rag-vs-eager-fetch)
> for the planned path forward in those cases.

---

## 8. Architecture overview

```
runner.py  _fetch_pr_context()
│
├── provider.get_pr_commits(owner, repo, pr_number)
│     └── CommitInfo(sha, message) list
│
├── ContextFetcher.build_context(pr_title, pr_description, commit_messages)
│     │
│     ├── extractor.extract_references(combined_text)
│     │     → list[Reference{ref_type, identifier, url, key}]
│     │
│     ├── [per Reference, in order, respecting token budget]
│     │     ├── GitHubIssuesFetcher.fetch_issue(owner, repo, number)
│     │     │     GET /repos/{owner}/{repo}/issues/{n}
│     │     │     GET /repos/{owner}/{repo}/issues/{n}/comments
│     │     │
│     │     ├── JiraFetcher.fetch_ticket(ticket_key)
│     │     │     GET /rest/api/2/issue/{key}?fields=summary,description,…
│     │     │
│     │     └── ConfluenceFetcher.fetch_page_by_id(page_id)
│     │           GET /wiki/rest/api/content/{id}?expand=body.storage,…
│     │
│     └── "<context>\n{joined items}\n</context>"
│
└── pr_context → create_review_agent(…, pr_context=pr_context)
      └── appends _CONTEXT_INSTRUCTION to system prompt
            → injected into user message for both modes:
              • single-shot:   diff + pr_context in one message
              • file-by-file:  pr_context appended to each per-file message
```

### New modules

| Module | Purpose |
|--------|---------|
| `src/code_review/context/__init__.py` | Package init; re-exports `ContextFetcher`, `Reference`, `ReferenceType`, `extract_references`. |
| `src/code_review/context/extractor.py` | Regex-based reference extraction. `extract_references(text, owner, repo, jira_project_keys)` → `list[Reference]`. |
| `src/code_review/context/fetcher.py` | `ContextFetcher.build_context(…)` — orchestrates extraction, fetching, deduplication, and token-budget enforcement. |
| `src/code_review/context/github_issues.py` | `GitHubIssuesFetcher` — fetches GitHub issue title, body, labels, comments via GitHub REST API. |
| `src/code_review/context/jira.py` | `JiraFetcher` — fetches Jira ticket fields and comments; handles Atlassian Document Format (ADF). |
| `src/code_review/context/confluence.py` | `ConfluenceFetcher` — fetches Confluence pages by ID or URL; HTML-to-plain-text conversion. |

### Modified files

| File | Change |
|------|--------|
| `src/code_review/config.py` | Added `ContextConfig` (env prefix `CONTEXT_`); updated `reset_config_cache()` and added `get_context_config()`. |
| `src/code_review/providers/base.py` | Added `CommitInfo` model; added default `get_pr_commits()` method (returns `[]`). |
| `src/code_review/providers/github.py` | Implemented `get_pr_commits()` via `GET /repos/{owner}/{repo}/pulls/{n}/commits`. |
| `src/code_review/agent/agent.py` | Added `pr_context` parameter to `create_review_agent()`; added `_CONTEXT_INSTRUCTION` constant appended to the system prompt when context is present. |
| `src/code_review/runner.py` | Added `_fetch_pr_context()` method; threaded `pr_context` through `_create_agent_and_runner()`, `_run_agent_and_collect_findings()`, `_run_single_shot_mode()`, and `_run_file_by_file_mode()`. |
| `.env.example` | Documented all `CONTEXT_*` environment variables. |

---

## 9. Extension — adding a new context source

The architecture is designed so that new sources (e.g. Linear, Shortcut, Notion) can be
added without touching the runner or the existing fetchers.

### Step 1 — Add a new `ReferenceType`

In `src/code_review/context/extractor.py`, add a new member to `ReferenceType` and extend
`extract_references()` with the corresponding regex pattern(s).

```python
class ReferenceType(str, Enum):
    GITHUB_ISSUE = "github_issue"
    JIRA         = "jira"
    CONFLUENCE   = "confluence"
    LINEAR       = "linear"          # new
```

### Step 2 — Create a fetcher module

Create `src/code_review/context/linear.py` with a class that accepts credentials and
returns a human-readable string for a given ticket ID:

```python
class LinearFetcher:
    def fetch_ticket(self, ticket_id: str) -> str:
        ...
```

### Step 3 — Wire into `ContextFetcher`

In `src/code_review/context/fetcher.py`:

1. Add a `_fetch_linear_ticket(self, ref)` method.
2. Dispatch to it in `_fetch_reference()`.

```python
def _fetch_reference(self, ref: Reference) -> str:
    if ref.ref_type == ReferenceType.GITHUB_ISSUE:
        return self._fetch_github_issue(ref)
    if ref.ref_type == ReferenceType.JIRA:
        return self._fetch_jira_ticket(ref)
    if ref.ref_type == ReferenceType.CONFLUENCE:
        return self._fetch_confluence_page(ref)
    if ref.ref_type == ReferenceType.LINEAR:       # new
        return self._fetch_linear_ticket(ref)      # new
    return ""
```

### Step 4 — Add configuration

Extend `ContextConfig` in `src/code_review/config.py` with the new credentials fields
(following the pattern of `jira_enabled`, `jira_url`, etc.) and document them in
`.env.example`.

### Step 5 — Add tests

Create `tests/context/test_linear.py` with mocked HTTP, following the pattern in
`tests/context/test_jira.py` or `tests/context/test_github_issues.py`.

For guidance on handling very large documents from your new source, see
[§10 Design considerations: RAG vs. eager fetch](#10-design-considerations-rag-vs-eager-fetch).

---

## 10. Design considerations: RAG vs. eager fetch

This section addresses two architectural questions that arise naturally as the
context-enrichment feature grows:

1. **Should we use RAG instead of injecting the full fetched content?**
2. **Should we cache fetched content rather than re-fetching from Jira/GitHub on every run?**

### 10.1 The current approach (eager fetch + inject)

The implementation fetches each referenced document exactly once per review run and
injects the combined text into the LLM prompt, capped by `CONTEXT_MAX_CONTEXT_TOKENS`.

**This is the right default.** Here's why:

- Jira tickets and GitHub issues are typically **small** — a few hundred to ~2 000 tokens
  each. Even a PR that references 5 tickets will rarely exceed 10 000 tokens of context.
- The 20 000-token default budget is larger than what most real-world PRs need, and modern
  LLMs (Gemini 2.5, GPT-4o, Claude 3) have 128K–1M token context windows that can absorb
  it without issue.
- The approach is **stateless**: no vector store, no embeddings API, no infrastructure
  beyond the existing SCM credentials. This matches the project's one-shot, no-long-running-
  service design.
- **Content relevance is already high**: because references are extracted from the PR itself
  (title, description, commit messages), every fetched item was explicitly linked to this
  change. Retrieval by semantic similarity rarely beats retrieval by explicit reference.

### 10.2 When the current approach has real limitations

The eager-inject model does have genuine failure modes worth knowing about:

| Scenario | Problem | Severity |
|----------|---------|---------|
| Confluence design docs (10 000–100 000+ tokens each) | Content exceeds budget; later documents are truncated or dropped | Medium–High if design docs are long |
| PRs referencing 20+ issues (e.g. a release summary PR) | Budget splits too thinly; each item is heavily truncated | Medium |
| Multiple PRs reference the same popular issue/ticket | Same content is re-fetched and re-processed on every run | Low (latency/cost) |
| Jira epics with 50+ comments | Comment thread exceeds per-item character limit | Low (already capped) |

For teams where any of the first two scenarios is common, a smarter retrieval strategy is
worth adding. For most teams starting with this feature, the default is fine.

### 10.3 RAG: what it would look like here

RAG for this use case means: instead of injecting the full text of referenced documents,
break them into chunks, embed each chunk, and at review time retrieve only the chunks that
are most semantically similar to the code changes.

#### Data flow

```
Per referenced document (once per run, or cached from a previous run)
│
├── Split into chunks (e.g. 300–500 token paragraphs, with ~50-token overlap)
│
├── Embed each chunk  →  embedding API (OpenAI, Vertex AI, etc.)
│
└── Store in an in-memory vector store  (or persistent cache — see §10.4)
          │
          ▼  At query time
Query = PR diff text (or per-file diff in file-by-file mode)
          │
          ▼
          Similarity search (cosine / dot-product)  →  top-k chunks
          │
          ▼
Inject only top-k chunks into the LLM prompt  (<context>…</context>)
```

#### What it changes architecturally

| Component | Current | With RAG |
|-----------|---------|----------|
| `ContextFetcher.build_context()` | joins fetched text, truncates to budget | chunks text, embeds, retrieves top-k against diff |
| Dependencies | `httpx` only | `httpx` + embedding client + vector-store library |
| API calls per run | SCM/Jira/Confluence reads | same reads + N×embedding API calls |
| Prompt size | up to `CONTEXT_MAX_CONTEXT_TOKENS` | k × chunk_size (bounded and predictable) |
| Infrastructure | stateless | stateless (in-memory) or persistent (cache) |

#### Implementation sketch

```python
# context/rag.py  (new optional module)

class RAGContextBuilder:
    """Build context via chunk-embed-retrieve instead of eager inject."""

    def __init__(self, embedding_fn, top_k: int = 8, chunk_tokens: int = 400):
        self._embed = embedding_fn
        self._top_k = top_k
        self._chunk_tokens = chunk_tokens
        self._chunks: list[str] = []
        self._embeddings: list[list[float]] = []

    def add_document(self, text: str) -> None:
        """Chunk a document and embed its chunks."""
        for chunk in _split_into_chunks(text, self._chunk_tokens):
            self._chunks.append(chunk)
            self._embeddings.append(self._embed(chunk))

    def retrieve(self, query: str) -> str:
        """Return top-k chunks most relevant to query, joined."""
        query_vec = self._embed(query)
        scored = sorted(
            zip(self._chunks, self._embeddings),
            key=lambda t: _cosine(query_vec, t[1]),
            reverse=True,
        )
        return "\n\n---\n\n".join(c for c, _ in scored[:self._top_k])
```

`ContextFetcher.build_context()` would call `rag_builder.add_document(fetched_text)` for
each reference, then call `rag_builder.retrieve(diff_text)` to obtain the final injected
context instead of the joined full text.

#### Gating behind config

Add a field to `ContextConfig`:

```python
rag_enabled: bool = False
rag_embedding_model: str = "text-embedding-3-small"
rag_top_k: int = 8
```

So the feature is entirely opt-in. The default remains the current eager-inject approach.

### 10.4 Caching: avoiding re-fetches on every run

The separate concern raised alongside RAG is that **the same Jira ticket or GitHub issue is
re-fetched on every PR review run** — even if the ticket hasn't changed.

This is purely a latency/rate-limit concern rather than a context-quality concern. A ticket
is typically ~1–3 HTTP requests and <100 ms; for most teams this is negligible. It becomes
meaningful when:

- A high-volume monorepo has 50+ PRs a day, all referencing the same 5 core issues.
- Jira has strict API rate limits or is accessed over a slow VPN.
- Confluence pages are large and slow to download.

#### Caching strategy

The cleanest approach is an optional **in-process TTL cache** in `ContextFetcher`:

```python
# In ContextFetcher.__init__:
self._cache: dict[str, tuple[str, float]] = {}   # key → (text, fetched_at)
self._cache_ttl: float = 300.0                    # 5 minutes

def _fetch_reference(self, ref: Reference) -> str:
    now = time.monotonic()
    if ref.key in self._cache:
        text, fetched_at = self._cache[ref.key]
        if now - fetched_at < self._cache_ttl:
            return text
    text = self._dispatch_fetch(ref)
    self._cache[ref.key] = (text, now)
    return text
```

This deduplicates re-fetches **within a single run** (already handled by the dedup logic
in the extractor) and **across runs** when the `ContextFetcher` instance is reused (e.g.
in a long-lived worker process).

For the common CI/CD case where the runner is a one-shot container, per-run caching
provides no benefit. A **persistent cache** (Redis, SQLite, or a local file) would help
there, but introduces state. Given the project's stateless design, a persistent cache is
a deployment concern: operators can already handle this by running the agent behind a
reverse proxy or in a long-lived process that keeps the in-process cache warm.

### 10.5 Recommended path forward

| Situation | Recommendation |
|-----------|---------------|
| Typical team: PRs reference 1–5 small issues | Current eager-inject approach is sufficient. No changes needed. |
| Long Confluence design docs | Increase `CONTEXT_MAX_CONTEXT_TOKENS` first. If documents are >20K tokens, add per-document summarisation (LLM pre-pass) before enabling RAG. |
| 20+ issues per PR or large epics | Enable RAG (§10.3) once the optional module is implemented. |
| High-volume CI with Jira rate limits | Add the in-process TTL cache in `ContextFetcher` (§10.4). No persistent storage required. |
| Very high-volume or multi-tenant | Use a shared persistent cache (Redis/SQLite) keyed by `(source_type, identifier, content_hash)` with a configurable TTL. |

The implementation order should be:

1. **Summarisation pre-pass** — easiest to add, no new dependencies, handles large
   individual documents without requiring an embedding API.
2. **In-process TTL cache** — a small change to `ContextFetcher`, zero infrastructure.
3. **RAG with in-memory vector store** — adds an embedding API dependency; appropriate
   once teams hit context-quality limits that summarisation cannot solve.
4. **Persistent vector store** — only if cross-run caching is needed and the team is
   willing to maintain the additional infrastructure.
