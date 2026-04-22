# Context-Aware Review — User Guide

This guide explains how to enable and use context-aware review in day-to-day CI runs.

Context-aware review lets the agent read linked work items (GitHub Issues, GitLab Issues,
Jira tickets, Confluence pages), distill them to a concise brief, and use that brief during
code review.

This feature is optional and disabled by default.

---

## Table of Contents

1. [What this feature does](#1-what-this-feature-does)
2. [Quick start](#2-quick-start)
3. [Supported reference patterns](#3-supported-reference-patterns)
4. [Environment variables](#4-environment-variables)
5. [How reviews behave when systems fail](#5-how-reviews-behave-when-systems-fail)
6. [Troubleshooting](#6-troubleshooting)
7. [Examples](#7-examples)

---

## 1. What this feature does

When enabled, the runner:

1. Scans PR title, PR description, and PR commit messages for references.
2. Fetches matching documents from enabled sources.
3. If `CONTEXT_AWARE_REVIEW_DB_URL` is not set, clamps fetched text to `CONTEXT_MAX_BYTES` and distills it directly.
4. If `CONTEXT_AWARE_REVIEW_DB_URL` is set, caches documents in PostgreSQL and chooses one of two paths:
   - Under size budget: distill all fetched text directly.
   - Over size budget: retrieve relevant chunks (RAG), then distill.
4. Adds the distilled brief to the review prompt as a `Linked Work Item Context` section with reviewer guidance.

If no references are found, review runs normally without context.

---

## 2. Quick start

### 2.1 Minimum setup (GitHub Issues only)

Use this when `SCM_PROVIDER=github` and you want the lightest setup.

```bash
CONTEXT_AWARE_REVIEW_ENABLED=true

# Recommended explicit enablement for clarity:
CONTEXT_GITHUB_ISSUES_ENABLED=true
```

The runner uses `SCM_TOKEN` for GitHub API access when `SCM_PROVIDER=github`.

### 2.2 Add Jira

```bash
CONTEXT_AWARE_REVIEW_ENABLED=true
CONTEXT_GITHUB_ISSUES_ENABLED=true

CONTEXT_ATLASSIAN_EMAIL=you@yourcompany.com
CONTEXT_ATLASSIAN_TOKEN=your_atlassian_api_token
CONTEXT_JIRA_ENABLED=true
CONTEXT_ATLASSIAN_URL=https://yourcompany.atlassian.net
```

### 2.3 Add Confluence

```bash
CONTEXT_ATLASSIAN_URL=https://yourcompany.atlassian.net
CONTEXT_CONFLUENCE_ENABLED=true
```

---

## 3. Supported reference patterns

References are extracted from:

- PR title
- PR description
- PR commit messages

### GitHub Issues

- `#123` (same-repo style, only in GitHub review context)
- `GH-123`
- `https://github.com/org/repo/issues/123`

### GitLab Issues

- `https://gitlab.com/group/repo/-/issues/55`
- Other GitLab issue URLs with project paths are supported.

### Jira

- `PROJ-123`
- `https://<jira-host>/browse/PROJ-123`

### Confluence

- `https://<host>/wiki/spaces/<space>/pages/<id>/...`
- `https://<host>/pages/viewpage.action?pageId=<id>`

Notes:

- Extraction is conservative and strips fenced code blocks before scanning.
- Duplicate references are deduplicated before fetching.

---

## 4. Environment variables

All variables are optional unless marked required by the source you enable.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_AWARE_REVIEW_ENABLED` | `false` | Master switch. |
| `CONTEXT_AWARE_REVIEW_DB_URL` | — | Optional PostgreSQL DSN. Enables document cache and RAG for oversized context. When omitted, linked documents are fetched and distilled directly. |
| `CONTEXT_GITHUB_ISSUES_ENABLED` | `false` | Enable GitHub issue fetching. |
| `CONTEXT_GITLAB_ISSUES_ENABLED` | `false` | Enable GitLab issue fetching. |
| `CONTEXT_ATLASSIAN_EMAIL` | — | Atlassian account email used for Jira and Confluence. |
| `CONTEXT_ATLASSIAN_TOKEN` | — | Atlassian API token used for Jira and Confluence. |
| `CONTEXT_JIRA_ENABLED` | `false` | Enable Jira fetching. |
| `CONTEXT_JIRA_EXTRA_FIELDS` | — | Comma-separated extra Jira fields (for custom acceptance criteria, etc.). |
| `CONTEXT_CONFLUENCE_ENABLED` | `false` | Enable Confluence fetching. |
| `CONTEXT_ATLASSIAN_URL` | — | Top-level Atlassian site URL used for Jira and Confluence, e.g. `https://yourcompany.atlassian.net` (not `/jira` or `/wiki`). |
| `CONTEXT_MAX_BYTES` | `20000` | Byte budget for context sent to distillation. Without DB, direct-mode input is clamped to this size; with DB/RAG enabled, over-budget context uses retrieval first. |
| `CONTEXT_DISTILLED_MAX_TOKENS` | `4000` | Max output tokens for distilled context brief. |
| `CONTEXT_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model used by RAG path. |
| `CONTEXT_EMBEDDING_DIMENSIONS` | `1536` | Embedding vector dimensions used for pgvector schema/search. |
| `CONTEXT_GITHUB_API_URL` | auto | Override GitHub API base when not using `SCM_PROVIDER=github`. |
| `CONTEXT_GITHUB_TOKEN` | — | GitHub token when SCM provider is not GitHub. |
| `CONTEXT_GITLAB_API_URL` | auto | Override GitLab API base when not using `SCM_PROVIDER=gitlab`. |
| `CONTEXT_GITLAB_TOKEN` | — | GitLab token when SCM provider is not GitLab. |

Related (not context-only):

| Variable | Default | Description |
|----------|---------|-------------|
| `CODE_REVIEW_INCLUDE_COMMIT_MESSAGES_IN_PROMPT` | `true` | Include commit-message summary block in review prompt. |

---

## 5. How reviews behave when systems fail

When context-aware review is enabled:

- Missing required config for an enabled source is fatal.
- Authentication/authorization errors (HTTP 401/403) are fatal.
- Other fetch failures for a reference (for example 5xx or transient network) are logged and that reference is skipped.
- If all references are skipped or nothing is resolved, review continues without context.

This prevents one temporarily unavailable external system from blocking all PR reviews.

---

## 6. Troubleshooting

### "Context-aware review configuration error"

Check required variables for every enabled source:

- Atlassian auth: `CONTEXT_ATLASSIAN_EMAIL`, `CONTEXT_ATLASSIAN_TOKEN`
- Jira and Confluence: `CONTEXT_ATLASSIAN_URL` as the top-level site URL, not `/jira` or `/wiki`
- GitHub/GitLab with non-matching SCM provider: source-specific token vars

### "No context attached" even with links in PR text

Verify:

- The reference pattern is one of the supported formats.
- The source is enabled (`CONTEXT_*_ENABLED=true`).
- The API credentials can access the referenced issue/page.

### Slow runs with large Confluence/Jira content

Tune:

- `CONTEXT_MAX_BYTES` (without DB: lower to clamp direct context sooner; with DB/RAG: lower to trigger RAG sooner)
- `CONTEXT_DISTILLED_MAX_TOKENS` (controls final brief size)

### Database errors

Only applies when `CONTEXT_AWARE_REVIEW_DB_URL` is configured.

Ensure:

- PostgreSQL reachable from runner.
- `pgvector` extension available.
- The configured embedding dimensions are consistent with your schema and model output.

---

## 7. Examples

### Example: GitHub + Jira + Confluence

```bash
CONTEXT_AWARE_REVIEW_ENABLED=true
CONTEXT_AWARE_REVIEW_DB_URL=postgresql://review:secret@postgres:5432/reviewdb

CONTEXT_GITHUB_ISSUES_ENABLED=true

CONTEXT_ATLASSIAN_EMAIL=review-bot@acme.com
CONTEXT_ATLASSIAN_TOKEN=${ATLASSIAN_API_TOKEN}

CONTEXT_JIRA_ENABLED=true
CONTEXT_ATLASSIAN_URL=https://acme.atlassian.net
CONTEXT_JIRA_EXTRA_FIELDS=customfield_10016,customfield_10014

CONTEXT_CONFLUENCE_ENABLED=true
```

### Example: Tight budget for noisy docs

```bash
CONTEXT_MAX_BYTES=12000
CONTEXT_DISTILLED_MAX_TOKENS=2500
```

This tends to produce shorter context briefs and can improve review prompt efficiency.
