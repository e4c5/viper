# Batch Review Execution Refactor

This document proposes a refactor of batch review execution for the code-review agent.

The current design uses a Google ADK `SequentialAgent` to run prepared review batches and then recovers from malformed output or rate limits by parsing the streamed final responses and retrying failed batches. That approach works for simple cases, but it couples orchestration, transport behavior, and recovery too tightly to ADK event streams.

The proposed design keeps ADK as the worker that reviews one prepared scope, while moving batch scheduling, retry policy, scope shrinking, and result aggregation into Python.

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Current Design](#2-current-design)
3. [Observed Failure Modes](#3-observed-failure-modes)
4. [Additional Investigation Findings](#4-additional-investigation-findings)
5. [Goals](#5-goals)
6. [Non-Goals](#6-non-goals)
7. [Proposed Architecture](#7-proposed-architecture)
8. [Execution Flow](#8-execution-flow)
9. [Retry and Recovery Policy](#9-retry-and-recovery-policy)
10. [Observability Changes](#10-observability-changes)
11. [Migration Plan](#11-migration-plan)
12. [Test Plan](#12-test-plan)
13. [Implementation Checklist](#13-implementation-checklist)
14. [Open Questions](#14-open-questions)

---

## 1. Problem Statement

Batch review currently relies on this shape:

- Python prepares diff batches.
- Python creates one ADK `SequentialAgent` containing one sub-agent per batch.
- Python calls `Runner.run_async(...)` once for the whole workflow.
- Python collects text-bearing final responses from each sub-agent.
- Python parses each response into `FindingsBatchV1`.
- Python retries failed batches based on parse failures or rate limits.

This design has two core weaknesses:

1. **The scheduler is inside ADK, but the recovery policy is in Python.**
   Python sees only the emitted events and final text responses after the fact. That makes it awkward to reason about which scopes completed successfully, which failed structurally, and which should be retried, split, or dropped.

2. **Malformed structured output is treated as a normal operational path.**
   Even with `output_schema=FindingsBatchV1`, tool-free batch agents still return text-bearing final responses, so malformed JSON can surface as truncation, invalid schema shape, or mixed partial output. Recovery then becomes “ask again with a sterner prompt,” which often does not change the underlying failure mode.

The result is a design that is harder to reason about, harder to test, and harder to extend with better retry, backoff, or scope-management behavior.

---

## 2. Current Design

The current batch-review path primarily involves:

- `src/code_review/orchestration/standard_review.py`
- `src/code_review/orchestration/execution.py`
- `src/code_review/agent/workflows.py`
- `src/code_review/orchestration/runner_utils.py`

Current flow:

1. `StandardReviewHandler._execute_review_agent(...)` computes batch budgets and builds prepared `ReviewBatch` values.
2. `execution.create_agent_and_runner(...)` builds a `SequentialAgent` from those batches.
3. `execution.run_agent_and_collect_findings(...)` invokes the workflow once.
4. `runner_utils._collect_final_response_texts_async(...)` gathers final responses per sub-agent author.
5. `execution.findings_from_batch_responses(...)` parses each response.
6. Recovery logic in `execution.py` retries malformed or rate-limited batches.

Strengths of the current design:

- Reuses ADK orchestration primitives.
- Allows partial preservation of successful batch responses.
- Keeps prepared diff segmentation in Python.

Weaknesses of the current design:

- ADK event collection is serving as the control plane for recovery.
- Recovery logic must infer batch state from `author` names and text payloads.
- Parse failures and rate limits are interleaved in one workflow run.
- A malformed large batch tends to be retried at nearly the same scope, even when the real problem is response size or complexity.

---

## 3. Observed Failure Modes

The current design has shown these real or likely failure modes:

- **Truncated JSON** due to long or interrupted model output.
- **Schema-invalid JSON** that parses as JSON but does not validate as `FindingsBatchV1`.
- **Mixed outcomes in one workflow run**, where some sub-agents succeed, one returns malformed output, and a later one hits a 429.
- **Recovery loops that preserve the original scope**, even though the batch itself is too large or semantically dense.
- **Difficult observability**, because logs describe event-stream symptoms instead of first-class batch-job states.

These are signs that the execution boundary is in the wrong place.

---

## 4. Additional Investigation Findings

Further investigation of the current implementation and the installed Google ADK version
(`1.28.1`) produced these important findings:

### 4.1 ADK Is Already Sending Native Structured Output for Tool-Free Agents

For tool-free `LlmAgent` runs, ADK's `SingleFlow` path sets:

- `response_schema`
- `response_mime_type = "application/json"`

through `LlmRequest.set_output_schema(...)` when `output_schema` is present.

That means the repeated malformed JSON in batch review is **not primarily caused by missing
schema configuration**. Native structured output is already requested.

Implication:

- The essential fix is **not** to manually stuff `response_schema` into
  `GenerateContentConfig`.
- The more likely causes are:
  - oversized / overly verbose output payloads
  - truncation caused by output-token limits or finish conditions
  - insufficient response diagnostics

### 4.2 The Batch Prompt Is Probably Too Output-Heavy

The current review prompt asks for or strongly encourages:

- `message`
- `evidence`
- `confidence`
- `anchor`
- `suggested_patch`
- `agent_fix_prompt`

and also includes verbose examples for `suggested_patch` and `agent_fix_prompt`.

Even though many of these fields are optional in the schema, the prompt makes them attractive
or mandatory in common cases. This substantially increases response size.

Observed logs show repeated truncation inside the first finding's `message` field, even after
scope splitting. That strongly suggests the model is trying to emit more structured content than
the response budget safely supports.

### 4.3 We Do Not Yet Log Enough Response Metadata

The current batch collectors log parse failures, but they do not systematically surface:

- `finish_reason`
- token usage / `usage_metadata`
- whether the final event ended due to max tokens
- response text length per batch

Without that, we can suspect truncation but cannot prove it reliably from runtime logs.

### 4.4 We Are Parsing Raw Text Even Though ADK Can Validate Output Into State

ADK's `LlmAgent` supports `output_key`, and when both `output_key` and `output_schema`
are set, ADK validates the final response and writes the parsed result into session state.

We currently do not use `output_key`, so even successful structured responses are consumed by
reparsing raw text from final events. That is unnecessary coupling to transport text for the
success path.

This does not solve truncation by itself, but it is a cleaner success-path integration and
should be part of the longer-term refactor.

### 4.5 Essential Near-Term Reliability Fixes

Before or alongside the coordinator refactor, the plan should include these essential fixes:

1. **Slim the batch-review output contract.**
   For batch review, return only the minimal fields needed to post a high-quality finding:
   - required: `path`, `line`, `severity`, `code`, `message`
   - allowed when concise and necessary: `end_line`, `anchor`
   - defer `evidence`, `confidence`, `suggested_patch`, and `agent_fix_prompt`
     from the batch-review path unless explicitly reintroduced under a separate, budgeted step

2. **Detect truncation explicitly.**
   Log `finish_reason`, usage metadata, and response text length for every batch execution.
   If the model stopped for a token-limit-like reason, classify the outcome as truncation and
   split the scope immediately instead of repeating the same batch with only prompt nudges.

3. **Use ADK's validated output path for success cases.**
   Add `output_key` for single-batch workers and prefer reading validated structured output from
   session state on success. Keep raw-text parsing as a diagnostic / fallback path.

4. **Separate “review finding” from “fix guidance.”**
   If we still want `suggested_patch` or `agent_fix_prompt`, add them in a second, targeted
   enrichment step for a small set of selected findings rather than requiring them during the
   primary batch review pass.

---

## 5. Goals

The refactor should:

- Make Python the owner of batch scheduling and recovery.
- Keep ADK responsible for reviewing one prepared scope at a time.
- Treat malformed structured output as a batch outcome, not a parsing side-effect.
- Make retry and scope shrinking explicit and deterministic.
- Preserve current user-visible behavior where practical:
  - prepared diff batching
  - findings-only agent behavior
  - refinement funnel
  - comment posting
  - quality gate / review-decision logic
- Improve testability and observability of batch review.

---

## 6. Non-Goals

This refactor does not aim to:

- Replace Google ADK entirely.
- Rewrite the findings schema.
- Change SCM provider contracts.
- Change deduplication, fingerprinting, or posting logic.
- Introduce distributed queues or external job infrastructure.
- Parallelize review execution immediately.

Parallel execution may become easier after this refactor, but it is not required for the first implementation.

---

## 7. Proposed Architecture

### 6.1 High-Level Shape

Use ADK as a **single-scope worker**, and use Python as the **batch coordinator**.

New conceptual split:

- **Batch planning**
  - existing logic: prepare `ReviewBatch` items from file diffs and token budgets
- **Batch execution**
  - one ADK invocation per prepared batch
- **Batch coordination**
  - Python queue / state machine
  - retry, split, backoff, drop
- **Post-processing**
  - existing refinement funnel and posting

### 6.2 New Coordinator

Introduce a new module:

- `src/code_review/orchestration/batch_coordinator.py`

Suggested responsibilities:

- Own a queue of prepared batches to execute.
- Track per-batch attempt count and outcome state.
- Call a single-batch execution function.
- Decide whether to:
  - mark success
  - retry same scope
  - split into smaller scopes
  - skip after exhausted retries
  - fail fast on fatal errors
- Aggregate findings from successful scopes.

### 6.3 Suggested Data Types

The exact names are flexible, but the design should include the equivalents of:

- `BatchJob`
  - `batch: ReviewBatch`
  - `attempt: int`
  - `parent_job_id: str | None`
  - `reason: str | None`

- `BatchOutcome`
  - `status: Literal["success", "malformed_output", "rate_limited", "fatal_error"]`
  - `findings: list[FindingV1]`
  - `error: Exception | None`
  - `response_metadata: dict[str, Any]`

- `BatchCoordinatorResult`
  - `findings: list[FindingV1]`
  - `completed_jobs: int`
  - `skipped_jobs: int`
  - `failed_jobs: int`

### 6.4 Single-Batch Execution

Refactor `execution.py` so its primary role is:

- build one review agent for one prepared batch
- run one ADK invocation
- parse the returned structured response
- classify the outcome

That is a much cleaner contract than “run a workflow of many sub-agents and infer what happened from all emitted events.”

---

## 7. Execution Flow

Proposed flow inside `StandardReviewHandler._execute_review_agent(...)`:

1. Compute batch budget and prompt suffix as today.
2. Build prepared `ReviewBatch` values as today.
3. Instantiate `BatchReviewCoordinator`.
4. Call `coordinator.run(...)`.
5. Receive merged `all_findings`.
6. Continue into the existing refinement funnel and posting flow unchanged.

### 7.1 New Execution Sequence

```text
StandardReviewHandler
  -> build_review_batches_for_scope(...)
  -> BatchReviewCoordinator.run(batches, prompt_suffix, context_brief_attached, ...)
       -> for each pending BatchJob:
            -> execution.run_single_batch(...)
                 -> create one review agent
                 -> Runner.run_async(...) once
                 -> parse findings
                 -> return BatchOutcome
            -> recovery policy decides next step
       -> return aggregated findings
  -> refine findings
  -> post comments / summaries
```

### 7.2 Important Boundary

The coordinator should not know about:

- deduplication
- fingerprinting
- provider posting
- PR summary generation
- quality gate review decisions

Those remain in existing orchestration and refinement layers.

---

## 8. Retry and Recovery Policy

The key principle is:

> Retry policy should be driven by batch outcomes, not by free-text event interpretation.

### 8.1 Outcome Classes

- `success`
  - valid `FindingsBatchV1`
- `malformed_output`
  - invalid JSON
  - top-level wrong type
  - schema validation failure
- `rate_limited`
  - provider/model surfaced rate limit
- `fatal_error`
  - authentication
  - invalid configuration
  - unexpected runtime error

### 8.2 Recovery Rules

For `malformed_output`:

1. If the batch can be split into smaller scopes, split it immediately.
2. If the batch cannot be split further, retry same scope up to `N` times.
3. If still malformed after max retries, skip and log clearly.

For `rate_limited`:

1. Retry same scope up to `N` times.
2. Optionally add short backoff / jitter later.
3. Do not split solely because of rate limit.

For `fatal_error`:

1. Fail the run immediately.
2. Do not continue with partial execution unless explicitly designed later.

### 8.3 Scope Shrinking Policy

Preferred order:

1. Split multi-segment batches into smaller batch groups.
2. If one large segment remains, re-segment the diff with a smaller budget.
3. If the segment cannot be split further, retry same scope with capped attempts.

This matches the operational intuition that malformed output is often caused by scope size or complexity, not by randomness alone.

---

## 10. Observability Changes

The refactor should improve observability by logging first-class batch states instead of inferring them from ADK event structure.

Suggested log events:

- `batch_job_start`
- `batch_job_success`
- `batch_job_malformed_output`
- `batch_job_rate_limited`
- `batch_job_split`
- `batch_job_retry`
- `batch_job_skip_exhausted`
- `batch_review_complete`

Useful fields:

- `trace_id`
- `job_id`
- `parent_job_id`
- `paths`
- `segment_count`
- `attempt`
- `status`
- `duration_ms`
- `findings_count`
- `split_count`
- `finish_reason`
- `response_text_len`
- `usage_metadata`

This will make it easier to answer:

- Which scopes fail most often?
- Are malformed outputs concentrated in large batches?
- How much work is being retried or split?
- Are rate limits or schema failures the dominant cause of recovery?
- Are malformed outputs actually token-limit truncations?

---

## 11. Migration Plan

Implement in small, reviewable steps.

### Phase 0: Immediate Reliability Mitigations

- Reduce the batch-review output contract to the smallest safe schema subset.
- Remove or strongly discourage `suggested_patch` and `agent_fix_prompt` in the primary
  batch-review pass.
- Add finish-reason and usage logging to single-batch execution.
- Add `output_key` and prefer validated ADK state output for successful responses.

These changes are compatible with the later coordinator refactor and reduce risk immediately.

### Phase 1: Introduce Coordinator Types

- Add `batch_coordinator.py`.
- Add internal result/job types.
- Keep current `SequentialAgent` flow unchanged.
- Unit test coordinator behavior in isolation if needed.

### Phase 2: Add Single-Batch Executor

- Refactor `execution.py` to expose a clean `run_single_batch(...)`.
- Keep current multi-batch code temporarily for compatibility.
- Ensure parsing and outcome classification are stable.

### Phase 3: Switch Standard Review to Coordinator

- Update `StandardReviewHandler._execute_review_agent(...)` to call the coordinator.
- Coordinator uses `run_single_batch(...)`.
- Preserve all existing downstream funnel behavior.

### Phase 4: Remove SequentialAgent Batch Workflow

- Remove or narrow `agent/workflows.py` if no longer needed for batch review.
- Delete obsolete multi-response collection and workflow-specific recovery code.
- Update tests that currently assert `SequentialAgent` construction.

### Phase 5: Improve Structured Output Reliability

Optional follow-up:

- Investigate whether single-batch workers can use a stronger ADK structured-output path.
- Keep this separate from the orchestration refactor so each change is easier to reason about.

---

## 12. Test Plan

### 11.1 Coordinator Unit Tests

Add focused tests for:

- single-batch success
- malformed output then split
- malformed output then same-scope retry
- rate limit then retry
- exhausted retries then skip
- fatal error then fail fast

### 11.2 Execution Tests

Test `run_single_batch(...)` for:

- valid findings response
- malformed JSON
- schema-invalid JSON
- rate limit propagation
- fatal error propagation

### 11.3 Integration Tests

Update orchestration tests so they assert:

- `StandardReviewHandler` uses the coordinator
- findings still flow through refinement and posting unchanged
- idempotency and empty-scope behavior still short-circuit correctly

### 11.4 Regression Coverage

Preserve regression coverage for:

- schema-invalid responses being treated as malformed output
- malformed batch recovery after partial success
- shrinking large or multi-segment batches
- no loss of successful findings during recovery
- finish-reason-based truncation classification
- minimal-schema batch responses remaining parseable under realistic output budgets

---

## 13. Implementation Checklist

### 13.0 Immediate Reliability Fixes

- [ ] Slim the batch-review prompt so the primary review pass requests only minimal finding fields
- [ ] Remove mandatory `agent_fix_prompt` from the batch-review path
- [ ] Remove or disable `suggested_patch` generation from the batch-review path
- [ ] Stop encouraging `evidence` and `confidence` by default in batch review
- [ ] Add per-batch logging for `finish_reason`
- [ ] Add per-batch logging for response length and usage metadata
- [ ] Add `output_key` to single-batch workers and read validated output from session state on success
- [ ] Add regression tests for truncation classification and minimal-schema responses

### 12.1 Coordinator Scaffold

- [ ] Add `src/code_review/orchestration/batch_coordinator.py`
- [ ] Define `BatchJob`
- [ ] Define `BatchOutcome`
- [ ] Define `BatchCoordinatorResult`
- [ ] Define coordinator queue / loop structure

### 12.2 Execution Refactor

- [ ] Add `run_single_batch(...)` in `src/code_review/orchestration/execution.py`
- [ ] Add outcome classification for success / malformed / rate-limited / fatal
- [ ] Keep current parse helpers, but narrow them to single-batch use
- [ ] Extract or reuse batch-splitting helpers
- [ ] Remove direct dependency on multi-batch ADK workflow from new execution path

### 12.3 Standard Review Integration

- [ ] Update `StandardReviewHandler._execute_review_agent(...)` to call the coordinator
- [ ] Preserve prompt-suffix and context-brief behavior
- [ ] Preserve batch planning via `build_review_batches_for_scope(...)`
- [ ] Preserve downstream refinement funnel unchanged

### 12.4 Cleanup

- [ ] Deprecate or remove `create_agent_and_runner(...)` for multi-batch review
- [ ] Deprecate or remove `run_agent_and_collect_findings(...)` as workflow orchestration
- [ ] Remove `SequentialAgent` dependency from batch review
- [ ] Remove obsolete multi-response collection code if no longer needed
- [ ] Remove or narrow `src/code_review/agent/workflows.py` for batch-review use

### 12.5 Tests

- [ ] Add coordinator unit tests
- [ ] Add single-batch executor tests
- [ ] Update standard review integration tests
- [ ] Remove tests that assert `SequentialAgent` construction for batch review
- [ ] Keep and adapt regression tests for malformed output and rate limits

### 12.6 Observability

- [ ] Add structured logs for batch job lifecycle
- [ ] Add coordinator-level completion log
- [ ] Ensure current run-level observability still emits correct totals
- [ ] Add explicit truncation / max-token diagnostics

### 12.7 Follow-Up Reliability Work

- [ ] Investigate ADK structured-output improvements for single-batch workers
- [ ] Evaluate optional backoff/jitter on rate-limited retries
- [ ] Decide whether parallel execution is desirable after the refactor

---

## 14. Open Questions

1. Should exhausted malformed batches be skipped silently, or should they contribute a run-level warning/summary artifact?
2. Should rate-limited batches use fixed retry counts only, or should we add time-based backoff in the first refactor?
3. Should the coordinator preserve original batch ordering in the final merged findings, or is stable aggregation by completion acceptable?
4. Do we want to retain any ADK workflow abstraction at all for batch review, or should batch review become fully Python-driven?
5. After the refactor, do we want bounded parallel execution for independent batches?
6. Should fix-guidance generation (`suggested_patch`, `agent_fix_prompt`) become a second-pass enrichment stage for selected findings only?
7. Do we want to cap the number of findings returned per batch as an additional payload guardrail, or should scope splitting alone be sufficient?

---

## Summary

The main change proposed here is simple:

- **Current**: ADK orchestrates many prepared batches, Python recovers from the event stream.
- **Proposed**: Python orchestrates many prepared batches, ADK reviews one batch at a time.

That shift puts retry, recovery, scope shrinking, and observability in the layer best suited to own them.
