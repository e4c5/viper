# Instructions for AI Agents

This file helps AI coding assistants (e.g. Cursor, Codex) work effectively on the **code-review-agent** project. It is also optional context for the review agent when it runs against repositories that contain an AGENTS.md (via `get_file_content`).

---

## Project summary

**code-review-agent**: An AI-driven code review tool for CI/CD. It reviews pull request diffs using a configurable LLM (Google ADK), returns structured findings, and posts inline comments via a pluggable SCM provider (Gitea, GitHub, GitLab, Bitbucket). The **runner** orchestrates; the **agent** only discovers issues (findings-only mode by default).

---

## Where things live

| Area | Location | Notes |
|------|----------|--------|
| **Entry point** | `src/code_review/__main__.py` | CLI: `code-review` → `run_review()` |
| **Orchestration** | `src/code_review/runner.py` | Config, provider, skip/idempotency, agent run, filter, post |
| **ADK agent** | `src/code_review/agent/agent.py` | `create_review_agent(provider, review_standards, findings_only)` |
| **Tools** | `src/code_review/agent/tools/gitea_tools.py` | Tools wrap `ProviderInterface`; used by ADK agent |
| **SCM** | `src/code_review/providers/` | `base.py` = interface; `gitea.py`, `github.py`, `gitlab.py`, `bitbucket.py`; `get_provider()` in `__init__.py` |
| **Config** | `src/code_review/config.py` | `SCMConfig`, `LLMConfig` (Pydantic Settings, `SCM_*`, `LLM_*` env) |
| **Model** | `src/code_review/models.py` | `get_configured_model()`, `get_context_window()`, `get_max_output_tokens()` |
| **Findings** | `src/code_review/schemas/findings.py` | `FindingV1` — contract for agent JSON output |
| **Diff** | `src/code_review/diff/` | Parser, position, fingerprint/marker for dedup and comments |
| **Standards** | `src/code_review/standards/` | Language/framework detection; review prompt fragments |
| **Formatters** | `src/code_review/formatters/comment.py` | `finding_to_comment_body()` |
| **Observability** | `src/code_review/observability.py` | Optional Prometheus/OTel; used only in runner |

Tests mirror `src/`: `tests/test_runner.py`, `tests/providers/`, `tests/runner/`, `tests/cli/`, etc.

---

## Conventions

- **Configuration**: All env-based; no `.env` loading by default. SCM: `SCM_PROVIDER`, `SCM_URL`, `SCM_TOKEN`, … LLM: `LLM_PROVIDER`, `LLM_MODEL`, … See `config.py` and `.env.example`.
- **New SCM**: Implement `ProviderInterface` in `providers/<name>.py`, register in `get_provider()` in `providers/__init__.py`, add tests under `tests/providers/test_<name>.py` with mocked HTTP.
- **Agent behavior**: Instruction and tools are in `agent/agent.py` and `agent/tools/`. Findings-only mode: agent has no post/get_existing tools; runner does filtering and posting.
- **Testing**: Use `MockProvider` or `MagicMock` for the provider; **patch `google.adk.runners.Runner`** so `run()` yields a final event with JSON findings (no real LLM). Run: `pytest` (exclude `tests/e2e` unless `RUN_E2E=1`).

---

## When editing

- **Runner flow** (`runner.py`): Preserve order (config → provider → skip → existing comments → idempotency → files → language → agent → run → parse → filter → post). Any new step should fit this sequence.
- **Provider interface** (`providers/base.py`): Adding a method? Implement it in all four providers (gitea, github, gitlab, bitbucket) or document the default.
- **Finding schema** (`schemas/findings.py`): `FindingV1` is the agent output contract. Backward-compatible changes only (optional fields, defaults).
- **Agent instruction** (`agent/agent.py`): Keep findings-only instruction clear that the agent must **not** post or fetch existing comments; the runner handles that.

---

## Documentation

- **README.md** — Quick start, config, Docker/CI, observability.
- **docs/DEVELOPER_GUIDE.md** — Full implementation guide: architecture, flow, modules, config, extension points, testing.

**ADK**: Runner builds an ADK Agent (model, instruction, tools from `agent/tools/`) and uses Runner + InMemorySessionService; it calls `Runner.run()` then parses the final response for a JSON array of findings. Tools delegate to the provider; the agent does not post or fetch comments.

---

## Quick commands

```bash
pip install -e ".[dev]"
pytest --ignore=tests/e2e
code-review --owner <owner> --repo <repo> --pr <n> --head-sha <sha> --dry-run
```
