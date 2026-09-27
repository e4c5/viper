# Contributing to viper

## Setup

```bash
uv sync --extra dev        # or: pip install -e ".[dev]"
pre-commit install         # optional; CI runs the same hooks
```

## Tests

```bash
pytest                       # unit/integration; e2e is excluded by default
RUN_E2E=1 pytest tests/e2e   # full-stack E2E (needs Docker/Podman)
```

Mock the provider (`MockProvider`/`MagicMock`) and patch
`google.adk.runners.Runner` — tests never call a real LLM or SCM. See
AGENTS.md "Conventions" and DEVELOPER_GUIDE.md §9 for mocking patterns.

Coverage gate: `--cov` must stay at or above 80% (`fail_under` in pyproject).

## Lint & types

```bash
ruff check src tests         # the codebase is not ruff-format formatted
mypy src                     # optional locally; only if mypy is installed
```

## Commits and PRs

Commit subjects are short, imperative, capitalised or not — e.g.
`Bump viper: per-run review knobs`, `Harden review dispatch: ...`, `Add
webhooks CI ...`. No strict convention is enforced; keep it one line and
describe behaviour, not mechanics.

## Layout

AGENTS.md is the authoritative map (runner flow order, provider interface,
agent/finding conventions). DEVELOPER_GUIDE.md has the full architecture walk.
CONFIGURATION-REFERENCE.md is the single env-var reference — update it when
you add a config option.

## Adding an SCM provider

1. Implement `ProviderInterface` in `providers/<name>.py`.
2. Register it in `get_provider()` in `providers/__init__.py`.
3. Declare an accurate `ProviderCapabilities` (labels, suggestions, resolving,
   review decisions, concurrent fetches).
4. Implement (or deliberately default) every `ProviderInterface` method — the
   default no-ops exist for optional behaviours only.
5. Add `tests/providers/test_<name>.py` with mocked HTTP; cover pagination
   termination, 404 handling, and malformed payloads.
6. Document the provider in docs/ and CONFIGURATION-REFERENCE.md.
