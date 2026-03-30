# Instructions for AI Agents

This file helps AI coding assistants (e.g. Cursor, Codex) work effectively on the **code-review-agent** project. It is also optional context for the review agent when it runs against repositories that contain an AGENTS.md (via `get_file_content`).

---

## Project summary

**code-review-agent**: An AI-driven code review tool for CI/CD. It reviews pull request diffs using a configurable LLM (Google ADK), returns structured findings, and posts inline comments via a pluggable SCM provider (Gitea, GitHub, GitLab, Bitbucket, Bitbucket Data Center). The **runner** orchestrates; the **agent** only discovers issues (findings-only mode by default).

---

## Where things live

| Area | Location | Notes |
|------|----------|--------|
| **Entry point** | `src/code_review/__main__.py` | CLI: `code-review` → `run_review()` |
| **Orchestration** | `src/code_review/runner.py` | Config, provider, skip/idempotency, agent run, filter, post |
| **ADK agent** | `src/code_review/agent/agent.py` | `create_review_agent(..., context_brief_attached=...)` |
| **Tools** | `src/code_review/agent/tools/gitea_tools.py` | Tools wrap `ProviderInterface`; used by ADK agent |
| **SCM** | `src/code_review/providers/` | `base.py` = interface; `gitea.py`, `github.py`, `gitlab.py`, `bitbucket.py`, `bitbucket_server.py`; `get_provider()` in `__init__.py` |
| **Config** | `src/code_review/config.py` | `SCMConfig`, `LLMConfig` (Pydantic Settings, `SCM_*`, `LLM_*` env); see `docs/CONFIGURATION-REFERENCE.md` for all variables |
| **Logging** | `src/code_review/logging_config.py` | Centralized logging configuration |
| **Model** | `src/code_review/models.py` | `get_configured_model()`, `get_context_window()`, `get_max_output_tokens()` |
| **Findings** | `src/code_review/schemas/findings.py` | `FindingV1` — contract for agent JSON output |
| **Reply dismissal** | `src/code_review/schemas/reply_dismissal.py`, `src/code_review/schemas/review_thread_dismissal.py`, `src/code_review/agent/reply_dismissal_agent.py`, `src/code_review/providers/base.py` (`get_review_thread_dismissal_context`, `post_review_thread_reply`, `get_bot_attribution_identity`) | Review-decision-only: `CODE_REVIEW_REPLY_DISMISSAL_ENABLED`; thread context + reply on **GitHub**, **GitLab**, **Bitbucket Cloud**, **Bitbucket Server** (not **Gitea**) |
| **Diff** | `src/code_review/diff/` | Parser, position, fingerprint/marker for dedup and comments |
| **Standards** | `src/code_review/standards/` | Language/framework detection; `prompts/` contains review prompt fragments |
| **Formatters** | `src/code_review/formatters/comment.py` | `finding_to_comment_body()` |
| **Observability** | `src/code_review/observability.py` | Optional Prometheus/OTel; used only in runner |
| **Context-aware review** | `src/code_review/context/` | Optional linked-issue/Jira/Confluence enrichment; see `docs/CONTEXT-AWARE-USER-GUIDE.md` and `docs/CONTEXT-AWARE-DEVELOPER-GUIDE.md` |

Tests mirror `src/`: `tests/test_runner.py`, `tests/providers/`, `tests/runner/`, `tests/cli/`, etc.

---

## Conventions

- **Configuration**: All env-based; no `.env` loading by default (matches `config.py`). **Single reference:** `docs/CONFIGURATION-REFERENCE.md`. SCM: `SCM_PROVIDER` (gitea, github, gitlab, bitbucket, bitbucket_server), `SCM_URL`, `SCM_TOKEN`, … LLM: `LLM_PROVIDER` (gemini, openai, anthropic, ollama, vertex, openrouter), `LLM_MODEL`, `LLM_API_KEY` (single key for the chosen provider). Optional context: `CONTEXT_*`, `CONTEXT_AWARE_REVIEW_*`, `CODE_REVIEW_INCLUDE_COMMIT_MESSAGES_IN_PROMPT` — see `docs/CONTEXT-AWARE-USER-GUIDE.md`, `docs/CONTEXT-AWARE-DEVELOPER-GUIDE.md`, and `.env.example`.
- **New SCM**: Implement `ProviderInterface` in `providers/<name>.py`, register in `get_provider()` in `providers/__init__.py`, add tests under `tests/providers/test_<name>.py` with mocked HTTP.
- **Agent behavior**: Instruction and tools are in `agent/agent.py` and `agent/tools/`. Findings-only mode: agent has no post/get_existing tools; runner does filtering and posting. For large diffs, the runner uses **single-shot mode** (tool-free) to reduce token usage.
- **Testing**: Use `MockProvider` or `MagicMock` for the provider; **patch `google.adk.runners.Runner`** so `run()` yields a final event with JSON findings (no real LLM). Run: `pytest` (exclude `tests/e2e` unless `RUN_E2E=1`).

---

## When editing

- **Runner flow** (`runner.py`): Preserve order (config → provider → skip → existing comments → idempotency → files → language → agent → run → parse → filter → post). Any new step should fit this sequence.
- **Provider interface** (`providers/base.py`): Adding a method? Implement it in all providers (gitea, github, gitlab, bitbucket, bitbucket_server) or document the default.
- **Finding schema** (`schemas/findings.py`): `FindingV1` is the agent output contract. Backward-compatible changes only (optional fields, defaults).
- **Agent instruction** (`agent/agent.py`): Keep findings-only instruction clear that the agent must **not** post or fetch existing comments; the runner handles that.

---

## Documentation

- **README.md** — Quick start, config, Docker/CI, observability.
- **docs/SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md** — Per-SCM approve / needs-work / merge checks vs `SCM_REVIEW_DECISION_*`; review-decision-only and reply-dismissal (end user).
- **docs/DEVELOPER_GUIDE.md** — Full implementation guide: architecture, flow, modules, config, extension points, testing.
- **docs/WHAT-STAYS-CUSTOM.md** — Guardrails for which product logic should remain outside ADK.

**ADK**: Runner builds an ADK Agent (model, instruction, tools from `agent/tools/`) and uses Runner + InMemorySessionService; it calls `Runner.run()` then parses the final response for a JSON array of findings. Tools delegate to the provider; the agent does not post or fetch comments.

---

## Quick commands

```bash
pip install -e ".[dev]"
pytest --ignore=tests/e2e
code-review --owner <owner> --repo <repo> --pr <n> --head-sha <sha> --dry-run
```
