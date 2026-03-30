# Architectural Refactor Plan: De-godding the Review Orchestration

This document outlines a comprehensive plan to refactor the code-review-agent's orchestration layer using Object-Oriented Programming (OOP) principles. The goal is to decompose the current "god files" (`orchestration_deps.py` and `review_orchestrator.py`) into focused, maintainable, and testable components.

## 0. Starting Point: What the Refactor Branch Already Did

The `refactor` branch completed the first major decomposition of the original monolithic `runner.py` (~3,760 lines). The following files were produced:

| File | Lines | Role |
|------|-------|------|
| `runner.py` | ~60 | Thin public entry point; delegates to `ReviewOrchestrator` |
| `review_orchestrator.py` | ~1,500 | `ReviewOrchestrator` class — orchestrates the full review flow |
| `review_execution.py` | ~270 | Agent creation and single-run execution helpers |
| `orchestration_deps.py` | ~1,860 | 50+ procedural helper functions and two small classes |
| `batching.py` | ~370 | `ReviewBatch` dataclass and batch-budget logic |
| `agent/workflows.py` | ~80 | ADK `SequentialAgent` factory for multi-batch review |
| `evals/` | — | New evaluation harness and corpus fixtures |

This is the baseline for the phases described below.

## 1. Problem Statement

While the initial split removed the monolith, it produced two new "god files":
- **`review_orchestrator.py` (~1,500 lines)**: Handles PR info fetching, file listing, language detection, batching, agent execution, and result collection — all in one `ReviewOrchestrator` class with ~50 methods.
- **`orchestration_deps.py` (~1,860 lines)**: A procedural "dumping ground" for 50+ helper functions covering diff parsing, comment posting, quality gates, and post-processing. Also houses `QualityGateReviewOutcome` and `PartialResponseCollectionError` classes inline.

This creates high cognitive load, makes unit testing difficult, and hinders the extensibility of the system.

## 2. Target Architecture (OOP Design)

We will introduce a set of domain-specific classes to replace procedural helpers.

### 2.1 `DiffAnalyzer`
**Responsibility**: All operations on unified diffs and file content.
**Key Methods**:
- `analyze_pr_diff(diff_text)`: Builds internal line indexes and visible line sets.
- `estimate_tokens(content)`: Token budget estimation logic.
- `get_hunk_at(path, line)`: Retrieves context for a specific line.
- `normalize_path(path)`: Standardizes SCM-specific path prefixes.

### 2.2 `CommentManager`
**Responsibility**: Managing the lifecycle of review comments on the SCM.
**State**:
- `ignore_set`: Track existing comments to avoid duplicates.
- `resolved_fingerprints`: Track which findings have already been addressed.
**Key Methods**:
- `load_existing_comments(provider)`: Hydrates state from SCM (extracts logic from `ReviewOrchestrator._load_existing_comments_and_markers`).
- `filter_duplicates(findings)`: Uses fingerprints and hashes to deduplicate (extracts logic from `ReviewOrchestrator._attach_fingerprints_and_filter_findings` and `orchestration_deps._build_ignore_set`).
- `post_inline_comments(findings)`: Handles batch/individual posting logic.
- `resolve_stale_comments()`: Logic for auto-closing outdated threads.

### 2.3 `QualityGate`
**Responsibility**: Evaluating the overall review outcome against configuration. Formalises the existing `QualityGateReviewOutcome` dataclass and the `_compute_quality_gate_review_outcome` helper currently in `orchestration_deps.py`.
**Key Methods**:
- `evaluate(findings)`: Returns a `QualityGateOutcome` (PASS/FAIL/WARNING).
- `submit_decision(provider, outcome)`: Translates outcome to SCM `APPROVE`/`REQUEST_CHANGES`.

### 2.4 `FindingRefinementPipeline`
**Responsibility**: Sanitizing and correcting LLM output via a series of filters.
**Filters**:
- `SelfRetractionFilter`: Removes findings where the LLM corrected itself in prose.
- `ContradictionFilter`: Drops findings directly contradicted by the diff context.
- `AnchorRelocator`: Corrects line numbers using anchor text fuzzy matching.
- `PatchValidator`: Strips suggested patches that don't match the target code.

### 2.5 `ReviewFilter`
**Responsibility**: Deciding whether a PR should be skipped before the review begins.
**Key Methods**:
- `should_skip(pr_info, cfg)`: Returns a skip reason string (or `None`) based on skip labels and title patterns.

## 3. Module Organization

Target directory structure under `src/code_review/` after all phases complete:

```
src/code_review/
├── runner.py               # Thin public entry point (unchanged)
├── batching.py             # Batch sizing (already extracted — no change needed)
├── orchestration/          # New package consolidating orchestration logic
│   ├── __init__.py
│   ├── orchestrator.py     # Simplified ReviewOrchestrator (moved from review_orchestrator.py)
│   ├── execution.py        # Agent runner helpers (moved from review_execution.py)
│   └── filter.py           # ReviewFilter class
├── diff/
│   ├── analyzer.py         # DiffAnalyzer class (new)
│   ├── line_index.py       # Line index helpers (new)
│   └── (existing files: parser.py, fingerprint.py, position.py)
├── comments/
│   ├── manager.py          # CommentManager class (new)
│   └── (formatters etc — formatters/comment.py stays in place)
├── quality/
│   ├── gate.py             # QualityGate class (new)
│   └── outcome.py          # QualityGateOutcome value object (new; replaces inline dataclass)
└── refinement/
    ├── pipeline.py         # FindingRefinementPipeline (new)
    └── filters/            # Individual filter implementations (new)
        ├── __init__.py
        ├── self_retraction.py
        ├── contradiction.py
        ├── anchor_relocator.py
        └── patch_validator.py
```

> **Note**: `agent/workflows.py` and `evals/` were added during the initial refactor and are not subject to this plan. They are stable as-is.

## 4. Implementation Steps

The refactor will be executed in four phases to maintain system stability.

### Phase 1: Pure Logic Extraction (The "Foundation")
1. Create `src/code_review/diff/analyzer.py` with a `DiffAnalyzer` class; migrate `_estimate_tokens` and `_normalize_path_for_anchor` from `orchestration_deps.py`.
2. Create `src/code_review/diff/line_index.py`; migrate `_build_diff_line_index` and `_build_per_file_line_index` from `orchestration_deps.py`.
3. Create `src/code_review/refinement/` package; migrate `_relocate_findings_by_anchor`, `_filter_self_retracted_finding_messages`, and `_filter_obviously_contradicted_findings` from `orchestration_deps.py`.
4. Update `orchestration_deps.py` to import and re-export the moved symbols for backward compatibility.

### Phase 2: State Delegation (The "Heart")
1. Create `src/code_review/comments/manager.py` with `CommentManager`; absorb the deduplication logic currently split between `ReviewOrchestrator._load_existing_comments_and_markers`, `ReviewOrchestrator._attach_fingerprints_and_filter_findings`, and `orchestration_deps._build_ignore_set`.
2. Create `src/code_review/quality/gate.py` with `QualityGate`; migrate `_compute_quality_gate_review_outcome` and the `QualityGateReviewOutcome` dataclass from `orchestration_deps.py`.

### Phase 3: Orchestrator Slimming and Module Consolidation
1. Create the `src/code_review/orchestration/` package.
2. Move `review_orchestrator.py` → `orchestration/orchestrator.py` and `review_execution.py` → `orchestration/execution.py`; add backward-compatible re-exports in the old locations.
3. Refactor `ReviewOrchestrator` to delegate to `DiffAnalyzer`, `CommentManager`, `QualityGate`, and `FindingRefinementPipeline` instead of calling private helper methods.
4. Extract PR skip logic into `ReviewFilter` (`orchestration/filter.py`).
5. Remove private method bloat from `ReviewOrchestrator` once delegation is complete.

### Phase 4: Final Cleanup
1. Delete `orchestration_deps.py` (all consumers now import from the domain-specific modules).
2. Delete the backward-compatibility shims added in Phases 1–3.
3. Update all imports across the codebase including tests.
4. Run the full test suite (including integration and E2E where available).
5. Update `docs/DEVELOPER_GUIDE.md` to reflect the new architecture.

> Status note: the implementation branch has completed the architectural delegation work, but the full Phase 4 cleanup remains open. `orchestration_deps.py`, `review_orchestrator.py`, and `review_execution.py` still exist as compatibility layers, and several tests still depend on `code_review.runner` or `code_review.orchestration_deps` as legacy import hubs.

## 5. Risk Mitigation

- **Test-Driven Refactor**: Every move must be followed by running the existing test suite (`pytest`).
- **Incremental Rollout**: Use temporary re-exports in `orchestration_deps.py` and in the old root-level module locations to avoid breaking `ReviewOrchestrator` and tests while dependencies are moving. Do not remove any symbol until all consumers are updated.
- **Star-Import Bridge**: `runner.py` currently uses `from code_review.orchestration_deps import *`. This star import must remain valid until Phase 4. Only remove it after all downstream consumers have been updated to use explicit imports from the new modules.
- **Test Import Stability**: Several tests import helpers directly from `code_review.runner` (which re-exports via the star import). These tests must be updated in Phase 4 to import from the new module locations before the star import is removed.
- **Public API Stability**: The `run_review()` function signature in `runner.py` must not change at any point during the refactor. It is the public contract used by CI scripts, CLI, and tests.
