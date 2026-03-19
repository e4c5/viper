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

### Handling large context (RAG)

The current implementation truncates to the configured token budget, which is sufficient for
most issue-tracker entries. If your source can return very large documents (e.g. long
Confluence pages, long Notion databases), consider one of these approaches before injecting
into the prompt:

- **Per-item truncation** (already implemented): each item is truncated to the remaining
  budget. Increase `CONTEXT_MAX_CONTEXT_TOKENS` and `LLM_CONTEXT_WINDOW` for large docs.
- **Summarisation**: call an LLM pre-pass to summarise the fetched content to a fixed budget
  before injecting it into the review prompt.
- **RAG (Retrieval-Augmented Generation)**: embed the fetched content in a vector store,
  then retrieve only the chunks most relevant to the changed files. This can be implemented
  as a pre-processing step in `ContextFetcher.build_context()` before the items are joined.
  The `httpx` dependency is already available; add a vector-store client (e.g. ChromaDB,
  pgvector) as an optional dependency and gate it behind a config flag.
