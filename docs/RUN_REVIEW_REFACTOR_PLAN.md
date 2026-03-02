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
      def run(self) -> ReviewResult:
          """Execute the full review flow in the same order as the legacy run_review()."""
          ...
  ```

The exact `ReviewResult` type will be whatever `run_review()` currently returns (bool, enum,
or structured result). The refactor must **not** change that signature.

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

## 4. Step‑By‑Step Refactor Plan

At each step below:

- Move logic from `run_review()` into a `ReviewOrchestrator` method with as few modifications as
  possible.
- **After the code change, immediately run the existing tests**:

  ```bash
  pytest --ignore=tests/e2e
  ```

- Only proceed to the next step when all tests pass.

### Step 0 – Introduce `ReviewOrchestrator` Shell

1. Add `ReviewOrchestrator` to `runner.py` with:
   - An `__init__` that captures configuration/CLI arguments (same as `run_review()` parameters).
   - A `run()` method that simply calls the existing module‑level `run_review()` logic for now
     (copy‑paste or delegation).
2. Update module‑level `run_review()` to be a thin façade that:
   - Instantiates `ReviewOrchestrator` with the same arguments.
   - Calls `orchestrator.run()` and returns its result.
3. Run:
   - `pytest --ignore=tests/e2e`

### Step 1 – Extract Config and Provider Setup

1. Inside `ReviewOrchestrator.run()`, extract the configuration and provider‑setup code into
   `_load_config_and_provider()`.
2. Ensure that:
   - Any exceptions are propagated exactly as before.
   - Logging messages and metric labels are unchanged.
3. Replace the inlined configuration section in `run()` with a call to the helper.
4. Run:
   - `pytest --ignore=tests/e2e`

### Step 2 – Extract Skip Logic and Existing State

1. Move skip‑decision code into `_determine_skip_reason()`.
2. Move comment‑fetch and fingerprint parsing into `_load_existing_comments_and_markers()`.
3. Move idempotency‑key computation and short‑circuit logic into
   `_compute_idempotency_and_maybe_short_circuit()`.
4. Wire these helpers into `run()` in the same order they previously appeared in `run_review()`.
5. Run:
   - `pytest --ignore=tests/e2e`

### Step 3 – Extract Files, Diffs, and Language Detection

1. Extract provider calls for PR files and diffs into `_fetch_pr_files_and_diffs()`.
2. Extract ignore‑set building and file filtering into `_build_ignore_set_and_filter_files()`,
   reusing existing `_build_ignore_set` and related helpers.
3. Extract language detection into `_detect_languages_for_files()`, reusing the current
   `detect_from_paths()` call.
4. Ensure that any early‑return conditions (e.g. "no files to review") are preserved.
5. Run:
   - `pytest --ignore=tests/e2e`

### Step 4 – Extract Agent and Runner Creation

1. Move the agent construction logic into `_create_agent_and_runner()`, reusing
   `create_review_agent(...)`.
2. Ensure that:
   - `findings_only=True` is still used for production.
   - Any logging about model/provider selection is preserved.
3. Replace the corresponding block in `run()` with a call to the new helper.
4. Run:
   - `pytest --ignore=tests/e2e`

### Step 5 – Extract Agent Execution and Findings Handling

1. Move the `Runner.run()` invocation and response parsing into
   `_run_agent_and_collect_findings()` (wrapping the existing
   `_run_agent_and_collect_response()` implementation as appropriate).
2. Move fingerprinting and filtering into `_attach_fingerprints_and_filter_findings()`, reusing
   `_fingerprint_for_finding` and any existing deduplication logic.
3. Move all comment posting and summary posting into `_post_findings_and_summary()`.
4. Ensure that:
   - Existing batch + per‑comment + summary fallback behaviour is preserved.
   - All log messages about posting success/failure are preserved.
5. Run:
   - `pytest --ignore=tests/e2e`

### Step 6 – Extract Final Observability and Result Construction

1. Move the observability / metrics recording code into
   `_record_observability_and_build_result()`.
2. Return the same `ReviewResult` from `ReviewOrchestrator.run()` as
   the legacy `run_review()` used to return.
3. Confirm that any log message keys and metric labels remain untouched.
4. Run:
   - `pytest --ignore=tests/e2e`

At this point, `run_review()` should be a thin façade, and the orchestration should be entirely
encapsulated in `ReviewOrchestrator`.

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

