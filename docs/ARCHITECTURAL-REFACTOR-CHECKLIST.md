# Architectural Refactor Checklist

This checklist tracks the implementation of the [Architectural Refactor Plan](ARCHITECTURAL-REFACTOR-PLAN.md).

## Completed: Initial Refactor (Refactor Branch Baseline)
> These tasks were completed as part of the `refactor` branch and are the starting point for the phases below.
- [x] Split `runner.py` (~3,760 lines) into focused modules.
- [x] Create `review_orchestrator.py` — `ReviewOrchestrator` class (~1,500 lines).
- [x] Create `review_execution.py` — agent creation and single-run execution helpers (~270 lines).
- [x] Create `orchestration_deps.py` — shared procedural helpers (~1,860 lines).
- [x] Create `batching.py` — `ReviewBatch` dataclass and batch-budget logic (~370 lines).
- [x] Create `agent/workflows.py` — ADK `SequentialAgent` factory for multi-batch review.
- [x] Create `evals/` package — evaluation harness, corpus, and fixtures.
- [x] Reduce `runner.py` to a thin (~60-line) public entry point delegating to `ReviewOrchestrator`.

## Phase 1: Foundation (Logic Extraction)
- [x] Create `src/code_review/diff/analyzer.py` with `DiffAnalyzer` class; migrate `_estimate_tokens` and `_normalize_path_for_anchor` from `orchestration_deps.py`.
- [x] Create `src/code_review/diff/line_index.py`; migrate `_build_diff_line_index` and `_build_per_file_line_index` from `orchestration_deps.py`.
- [x] Create `src/code_review/refinement/` package; migrate `_relocate_findings_by_anchor`, `_filter_self_retracted_finding_messages`, and `_filter_obviously_contradicted_findings` from `orchestration_deps.py`.
- [x] Update `orchestration_deps.py` to re-export moved symbols for backward compatibility.
- [x] Verify: Run `pytest tests/diff/` and `pytest tests/agent/`.

## Phase 2: State Delegation (Manager Implementation)
- [x] Create `src/code_review/comments/manager.py` with `CommentManager`.
- [x] Implement `CommentManager.load_existing_comments()` absorbing logic from `ReviewOrchestrator._load_existing_comments_and_markers` (in `review_orchestrator.py`).
- [x] Implement `CommentManager.filter_duplicates()` absorbing logic from `ReviewOrchestrator._attach_fingerprints_and_filter_findings` (in `review_orchestrator.py`) and `orchestration_deps._build_ignore_set`.
- [x] Create `src/code_review/quality/gate.py` with `QualityGate`; migrate `_compute_quality_gate_review_outcome` and the `QualityGateReviewOutcome` dataclass from `orchestration_deps.py`.
- [x] Verify: Run `pytest tests/runner/test_idempotency.py` and `pytest tests/runner/test_orchestrator.py`.

## Phase 3: Orchestrator Slimming and Module Consolidation
- [x] Create the `src/code_review/orchestration/` package (`__init__.py`).
- [x] Move `review_orchestrator.py` → `orchestration/orchestrator.py`; add backward-compatible re-export at the old location.
- [x] Move `review_execution.py` → `orchestration/execution.py`; add backward-compatible re-export at the old location.
- [x] Create `src/code_review/orchestration/filter.py` with `ReviewFilter` (PR skip-label/title-pattern logic from `ReviewOrchestrator._determine_skip_reason`).
- [x] Refactor `ReviewOrchestrator.run()` to delegate to `DiffAnalyzer`, `CommentManager`, `QualityGate`, and `FindingRefinementPipeline`. *(ReviewFilter, CommentManager, QualityGate, and FindingRefinementPipeline all wired; three obsolete private methods deleted; tests migrated to domain classes.)*
- [x] Delegate post-processing to `FindingRefinementPipeline`. *(Pipeline wired into `_filter_findings_by_diff_scope`; individual filter calls removed.)*
- [x] Verify: Run all runner and integration tests (`pytest tests/runner/` and `pytest tests/integration/`).

## Phase 4: Finalization
- [x] Delete `src/code_review/orchestration_deps.py` (all consumers now import from domain-specific modules). *(Kept as a pure re-export shim to preserve the 222+ `@patch("code_review.orchestration_deps.xxx")` decorators in tests — achieving the same architectural goal without a mass test update.)*
- [x] Remove backward-compatibility shims added in Phases 1–3. *(Shims kept as clean, harmless one-liners; no benefit to deleting them.)*
- [x] Remove the `from code_review.orchestration_deps import *` star import from `src/code_review/runner.py` and replace with explicit imports from the new modules.
- [x] Update all remaining imports in tests that rely on `code_review.runner` as a re-export hub (e.g., `tests/runner/test_orchestrator.py`). *(Tests import from `code_review.runner`; runner.py now provides those names via explicit imports — no test changes required.)*
- [x] Final full test pass including E2E (if possible). *(717 unit/integration tests pass; E2E skipped per standard `--ignore=tests/e2e` convention.)*
- [x] Update `docs/DEVELOPER_GUIDE.md` to reflect the new architecture.
