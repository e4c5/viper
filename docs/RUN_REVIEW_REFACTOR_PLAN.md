## Refactor Plan — `run_review()` → `ReviewOrchestrator`

This document describes the concrete plan for refactoring `run_review()` in `runner.py` into a
clearer orchestration abstraction (a `ReviewOrchestrator` class plus small helpers), while **strictly
preserving the existing behaviour and flow** described in `AGENTS.md` and
`docs/IMPROVEMENT_PLAN.md` §2.2.

The intent is to make the orchestration easier to read, reason about, and extend, without changing
public APIs or semantics.

---

## 1. Goals and Non‑Goals

- **Goals**
  - **Preserve behaviour:** Keep the observable behaviour of `run_review()` identical:
    - CLI UX and options remain the same.
    - Runner flow order (config → provider → skip → existing comments → idempotency →
      files → language → agent → run → parse → filter → post) is unchanged.
    - Idempotency and deduplication semantics do not change.
    - Logging and observability signals remain compatible (field names, log levels, metrics).
  - **Improve structure:**
    - Replace the current ~300‑line monolithic `run_review()` with a cohesive
      `ReviewOrchestrator` abstraction and focused private helpers.
    - Make it easier to test individual orchestration stages in isolation.
  - **Strengthen tests:**
    - After **each extraction step**, run the existing test suite to confirm no regressions.
    - Once the main refactor is complete, add **new unit tests** that directly exercise the
      extracted methods on `ReviewOrchestrator`.

- **Non‑Goals (for this refactor)**
  - No new features (e.g. no new configuration flags, no changes to language detection, no new
    provider capabilities).
  - No changes to the agent instruction, tool set, or findings schema.
  - No architectural shifts such as async I/O, external session stores, or new observability
    backends.

---

## 2. Current Responsibilities of `run_review()`

Summarising `docs/IMPROVEMENT_PLAN.md` §2.2 and `AGENTS.md`, the current `run_review()` performs:

1. **Configuration and provider setup**
   - Load `SCMConfig` and `LLMConfig`; validate environment.
   - Instantiate the appropriate `ProviderInterface` implementation.
2. **Skip logic and existing state**
   - Evaluate skip conditions (labels, draft/WIP status, etc.).
   - Fetch existing review comments and any prior fingerprint markers.
   - Compute idempotency key and decide whether this review should run or be considered a no‑op.
3. **Diff, files, and language detection**
   - Fetch PR files and diffs.
   - Build ignore sets and filter files.
   - Run language detection (currently single‑language `detect_from_paths()`).
4. **Agent construction and session management**
   - Build the ADK agent using `create_review_agent(...)`.
   - Instantiate `Runner` and `InMemorySessionService`.
5. **Run agent and collect findings**
   - Prepare the review prompt (diff context, standards, metadata).
   - Call `Runner.run()` and parse the final response into `FindingV1` instances.
6. **Fingerprinting, filtering, and posting**
   - Attach fingerprints to findings (for deduplication and idempotency).
   - Filter out ignored/duplicate findings.
   - Post inline comments and PR summary comments, with batch → per‑comment → summary fallback.
7. **Observability and final result**
   - Emit metrics and structured logs.
   - Return an appropriate status / result to the CLI caller.

The refactor must preserve this sequence and behaviour.

---

## 3. Target `ReviewOrchestrator` Design

We will introduce a `ReviewOrchestrator` class in `runner.py` that encapsulates the end‑to‑end
review flow. The public API will mirror the existing `run_review()` parameters and return value.

### 3.1 Public Entry Points

- **Module‑level function (backwards‑compatible façade)**

  ```python
  def run_review(...):
      """
      Existing public entrypoint for running a review.

      This remains the function that the CLI and any external callers use.
      """
      orchestrator = ReviewOrchestrator(...)
      return orchestrator.run()
  ```

- **Orchestrator method**

  ```python
  class ReviewOrchestrator:
      def run(self) -> list[FindingV1]:
          """Execute the full review flow in the same order as the legacy run_review()."""
          ...
  ```

The return type of `run_review()` is **`list[FindingV1]`** (findings that were posted or would be
posted when `dry_run=True`). The refactor must **not** change that signature.

### 3.2 Proposed Internal Methods

These methods will be extracted from the existing `run_review()` body with minimal logic changes:

- **Configuration / provider**
  - `_load_config_and_provider()`
    - Load `SCMConfig` and `LLMConfig`.
    - Instantiate `ProviderInterface` and capture `ProviderCapabilities`.

- **Skip and prior state**
  - `_determine_skip_reason()`
    - Evaluate skip conditions (labels, draft/WIP, etc.).
  - `_load_existing_comments_and_markers()`
    - Fetch existing review comments.
    - Parse fingerprint markers from existing comments.
  - `_compute_idempotency_and_maybe_short_circuit()`
    - Build idempotency key.
    - Decide whether to skip the run based on existing fingerprints and config.

- **Files, diffs, and languages**
  - `_fetch_pr_files_and_diffs()`
    - Get PR files and diffs from the provider.
  - `_build_ignore_set_and_filter_files()`
    - Reuse `_build_ignore_set` and related helpers.
  - `_detect_languages_for_files()`
    - Call current language detector (single‑language for now).

- **Agent and session**
  - `_create_agent_and_runner()`
    - Build the findings‑only agent via `create_review_agent(...)`.
    - Instantiate ADK `Runner` and `InMemorySessionService`.

- **Execution and findings**
  - `_run_agent_and_collect_findings()`
    - Wrap the logic currently in `_run_agent_and_collect_response()` and the JSON parsing.

- **Fingerprinting, filtering, and posting**
  - `_attach_fingerprints_and_filter_findings()`
    - Attach fingerprints using `_fingerprint_for_finding`.
    - Filter duplicates / ignored paths.
  - `_post_findings_and_summary()`
    - Post inline comments with batch + per‑comment fallback.
    - Post PR summary comment.

- **Finalisation**
  - `_record_observability_and_build_result()`
    - Call observability hooks.
    - Construct and return the final result.

These method names are indicative; the actual naming can be tuned for clarity, but each method
should encapsulate a **single, testable responsibility**.

---

## 4. Step‑By‑Step Refactor Plan (Checklist)

At each step below:

- **[ ]** Move logic from `run_review()` into a `ReviewOrchestrator` method with as few modifications
  as possible.
- **[ ]** After the code change, immediately run the existing tests:

  ```bash
  pytest --ignore=tests/e2e
  ```

- **[ ]** Only proceed to the next step when all tests pass.

### Step 0 – Introduce `ReviewOrchestrator` Shell

- **[x]** Add `ReviewOrchestrator` to `runner.py` (init + `run()` delegating to existing logic).
- **[x]** Update module‑level `run_review()` to be a thin façade that instantiates the orchestrator and calls `run()`.
- **[x]** Run `pytest --ignore=tests/e2e` and ensure all tests pass.

### Step 1 – Extract Config and Provider Setup

- **[x]** Extract config/provider setup into `_load_config_and_provider()`.
- **[x]** Verify exceptions, logging, and metrics behave exactly as before.
- **[x]** Replace the inlined section in `run()` with a call to the helper.
- **[x]** Run `pytest --ignore=tests/e2e` and ensure all tests pass.

### Step 2 – Extract Skip Logic and Existing State

- **[x]** Move skip logic into `_determine_skip_reason()`.
- **[x]** Move comment fetching + fingerprint parsing into `_load_existing_comments_and_markers()`.
- **[x]** Move idempotency/short‑circuit into `_compute_idempotency_and_maybe_short_circuit()`.
- **[x]** Wire helpers into `run()` in the same order as before.
- **[x]** Run `pytest --ignore=tests/e2e` and ensure all tests pass.

### Step 3 – Extract Files, Diffs, and Language Detection

- **[ ]** Extract PR files/diffs into `_fetch_pr_files_and_diffs()`.
- **[ ]** Extract ignore‑set + file filtering into `_build_ignore_set_and_filter_files()`.
- **[ ]** Extract language detection into `_detect_languages_for_files()`.
- **[ ]** Ensure early‑return conditions (e.g. “no files to review”) are preserved.
- **[ ]** Run `pytest --ignore=tests/e2e` and ensure all tests pass.

### Step 4 – Extract Agent and Runner Creation

- **[ ]** Move agent construction into `_create_agent_and_runner()`.
- **[ ]** Ensure `findings_only=True` and logging/model selection behaviour are unchanged.
- **[ ]** Replace inlined block in `run()` with the helper call.
- **[ ]** Run `pytest --ignore=tests/e2e` and ensure all tests pass.

### Step 5 – Extract Agent Execution and Findings Handling

- **[ ]** Move `Runner.run()` + response parsing into `_run_agent_and_collect_findings()`.
- **[ ]** Move fingerprinting/filtering into `_attach_fingerprints_and_filter_findings()`.
- **[ ]** Move posting logic into `_post_findings_and_summary()`.
- **[ ]** Verify batch/per‑comment/summary fallback and logging are unchanged.
- **[ ]** Run `pytest --ignore=tests/e2e` and ensure all tests pass.

### Step 6 – Extract Final Observability and Result Construction

- **[ ]** Move observability/metrics into `_record_observability_and_build_result()`.
- **[ ]** Ensure the return type is `list[FindingV1]` and semantics match the legacy `run_review()`.
- **[ ]** Confirm log keys and metric labels are unchanged.
- **[ ]** Run `pytest --ignore=tests/e2e` and ensure all tests pass.

### Final

- **[ ]** Confirm `run_review()` is now a thin façade over `ReviewOrchestrator.run()` and all tests are green.

---

## 5. Testing Strategy

### 5.1 Regression Safety via Existing Tests

- After **each** extraction step (0–6), run:

  ```bash
  pytest --ignore=tests/e2e
  ```

- If any existing test fails:
  - Prefer **adjusting the refactor** to preserve behaviour instead of changing tests.
  - Only update tests where they are clearly relying on implementation details that are no longer
    relevant (e.g. exact log message text, but not semantics).

### 5.2 New Tests Targeting Extracted Methods

Once the refactor stabilises and all existing tests are green:

1. **Add tests for `ReviewOrchestrator.run()` happy path and key error paths**, mirroring the
   coverage that currently exists for `run_review()`.
2. **Add focused unit tests for selected helpers**, for example:
   - `_determine_skip_reason()`:
     - Different combinations of labels (including `SCM_SKIP_LABEL`), draft/WIP, etc.
   - `_load_existing_comments_and_markers()`:
     - PRs with no comments.
     - PRs with comments containing valid fingerprint markers.
     - PRs with forged/invalid markers (ensure they are ignored if already hardened).
   - `_attach_fingerprints_and_filter_findings()`:
     - Duplicate findings across runs.
     - Findings that should be ignored due to ignore rules.
   - `_post_findings_and_summary()`:
     - Provider failures triggering fallback posting behaviour.
3. Use the existing patterns recommended in `AGENTS.md` and the improvement plan:
   - Mock providers via `MockProvider` or `MagicMock`.
   - Patch `google.adk.runners.Runner` so that `run()` yields a final event with JSON findings,
     avoiding real LLM calls.

Run the full suite (excluding optional E2E tests) after adding new tests:

```bash
pytest --ignore=tests/e2e
```

---

## 6. Follow‑Ups and Future Extensions

The `ReviewOrchestrator` abstraction should make it easier to implement future improvements
described elsewhere in `docs/IMPROVEMENT_PLAN.md`, for example:

- Plugging in **monorepo‑aware language detection** (`detect_from_paths_per_folder_root()`) inside
  `_detect_languages_for_files()` without bloating `run_review()`.
- Introducing a **real timeout** around `_run_agent_and_collect_findings()` using
  `LLM_TIMEOUT_SECONDS`.
- Evolving the runner to support **async HTTP calls** or parallel file fetching, while keeping
  the high‑level orchestration readable and testable.

These are **out of scope** for the initial refactor but are made easier by the structure described
in this document.