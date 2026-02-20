# Phase 1: Why Some Items Are Unchecked and Blockers

This document explains each **unchecked** Phase 1 checklist item: why it wasn’t implemented (yet) and whether there are blockers.

---

## 1.5 Diff Parser

| Item | Status | Why not done | Blocker? |
|------|--------|--------------|----------|
| Commentable positions: map `(path, line_in_new_file)` to hunk index and API-specific coordinates | Unchecked | **Already implemented** in `src/code_review/diff/position.py`: `get_commentable_positions()` and `CommentablePosition` (path, line_in_new_file, hunk_index, api_coords). Checklist is out of date. | **No** — Can mark done. |
| Provider adapters convert internal representation to SCM API payload | Unchecked | Gitea’s `post_review_comments` builds the API payload from `(path, line, body)` tuples only. It does **not** take `CommentablePosition` or use diff hunk index. So the “internal representation” (CommentablePosition) is not used when posting. | **No** — Optional refinement: pass positions into provider or have provider accept positions for APIs that need hunk/position. |

**Summary:** First bullet can be marked complete. Second is a design choice (use positions in provider layer); no blocker.

---

## 1.8 Runner (token budget and chunking)

| Item | Status | Why not done | Blocker? |
|------|--------|--------------|----------|
| Runner invokes agent with pre-chunked diff when over token budget | Unchecked | Runner always sends one user message and runs the agent once. No token counting or chunking. | **No** — `get_context_window()` exists in `models.py`; not used in runner yet. |
| Token budget check via LLM_CONTEXT_WINDOW | Unchecked | Same as above: no comparison of diff size (or estimated tokens) to `get_context_window()`. | **No** — Config and helper exist; need to add logic in runner. |
| File-by-file loop when diff exceeds threshold | Unchecked | No file-by-file invocation; single agent run over full PR. | **No** — Can use `get_pr_diff_for_file` and loop over files when over budget. |

**Summary:** Deferred to keep Phase 1 minimal (single run, no chunking). No blockers; requires: estimate diff tokens (e.g. chars/4), compare to `get_context_window()`, and if over threshold run agent per file and merge findings.

---

## 1.10 Language/Framework Detector

| Item | Status | Why not done | Blocker? |
|------|--------|--------------|----------|
| Confidence as 0.0–1.0 with thresholds | Unchecked | `DetectedContext.confidence` is `Literal["high", "medium", "low"]`, not numeric. Checklist asks for a numeric confidence and thresholds. | **No** — Would require API change (new field or replace literal) and threshold rules. Optional enhancement. |
| Monorepo mode: detect per file and per folder root | Unchecked | Detection is per-repo (paths list), not per-file or per-folder. | **No** — Would need to group paths by folder, run detector per group, and possibly return multiple contexts. Design/scope choice. |

**Summary:** Both are enhancements; no blocker. Confidence 0.0–1.0 is a small API/implementation change; monorepo is a larger behavior/design change.

---

## 1.12 Repo-Content Safety

| Item | Status | Why not done | Blocker? |
|------|--------|--------------|----------|
| System instruction immutable; repo content cannot override tool rules | Unchecked | Today repo content (e.g. AGENTS.md) is **not** injected into the system instruction; the agent fetches it via `get_file_content` (tool response). System instruction is built only from `BASE_INSTRUCTION` / `FINDINGS_ONLY_INSTRUCTION` + `review_standards`. So repo content cannot override tool rules. | **No** — Largely already true. Can mark done and/or add a short doc or comment that “system instruction is set only by app code; repo content is tool payload only.” |

**Summary:** Behavior already aligns with “system instruction immutable.” No blocker; optional doc/comment to make it explicit.

---

## Phase 1 Tests (unchecked)

| Item | Status | Why not done | Blocker? |
|------|--------|--------------|----------|
| `tests/standards/test_detector.py` — extensions, frameworks, confidence | Unchecked | Not written. | **No** — Straightforward tests for `detect_from_paths`, `detect_from_paths_and_content`, `_extract_*_frameworks`. |
| `tests/models/test_model_factory.py` — get_configured_model per provider | Unchecked | Not written. May require env or mocks for each provider. | **No** — Can mock config and assert model type/string. |
| `tests/tools/test_scm_tools.py` — tools call provider correctly | Unchecked | Not written. | **No** — Create tools with mock provider, invoke, assert provider methods called with expected args. |
| `tests/agent/test_runner.py` — file-by-file, ignore list, posts net-new | Unchecked | test_runner.py has basic agent creation; no test for full run with ignore list / posting. File-by-file not implemented in runner yet. | **Partial** — Ignore list and “posts net-new” can be tested with mocked runner/provider now. File-by-file tests depend on implementing the file-by-file runner path first. |

**Summary:** No blockers for detector, model factory, or tools tests. Runner tests can cover current behavior now; file-by-file tests after that feature exists.

---

## Recommended next steps (no blockers)

1. **Checklist update:** Mark “Commentable positions” (1.5 first bullet) and “System instruction immutable” (1.12) done if you agree they’re satisfied.
2. **Runner token budget and chunking (1.8):** Add token budget check and optional file-by-file loop when diff exceeds threshold (no blockers).
3. **Tests:** Add `tests/standards/test_detector.py`, `tests/models/test_model_factory.py`, `tests/tools/test_scm_tools.py`, and expand `tests/test_runner.py` for ignore list and posting behavior.
4. **Optional:** Confidence as 0.0–1.0 (1.10), provider using CommentablePosition (1.5 second bullet), monorepo detector (1.10)—all design/scope, no technical blockers.
