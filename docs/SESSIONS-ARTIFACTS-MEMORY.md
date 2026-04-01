# Sessions, Artifacts, and Memory

This document records the Phase 5 decisions from the ADK adoption plan.

## Scope

Phase 5 is about whether the ADK runtime should take on more stateful responsibilities:

- session creation and session state
- artifact storage
- memory services
- persistent session backends for resumability

The goal here is to simplify the runtime where it helps, while avoiding stateful complexity that does not currently improve the product.

## Current Decision Summary

- Use `Runner(auto_create_session=True)` for local runtime simplification.
- Do not adopt meaningful session state for the current review flow.
- Do not adopt `artifact_service` yet.
- Defer `memory_service` until a real cross-run use case exists.
- Do not adopt persistent session backends unless resumable service-mode review jobs become necessary.

## Session Creation

The review flow is still a one-shot run:

- prepare review batches
- run the ADK agent workflow
- collect findings
- filter and post via the SCM provider

Because of that, the only useful Phase 5 runtime change today is letting ADK create in-memory sessions automatically.

Current implementation:

- review runners now use `Runner(..., auto_create_session=True)`
- reply-dismissal runs use `Runner(..., auto_create_session=True)`
- local eval ADK runs use `Runner(..., auto_create_session=True)`

This removes a bit of wrapper glue without changing product behavior.

## Session State

Decision: no meaningful session state for now.

Reason:

- the product is still fundamentally one-shot per review invocation
- findings are collected directly from the run response, not from session state
- we are not chaining multi-step agent decisions through stored ADK state
- introducing session-state dependence would increase debugging and operational complexity with little payoff

We can revisit this only if the workflow becomes genuinely stateful across steps or across runs.

## Artifacts

Decision: not now.

Potential future uses:

- storing distilled context briefs
- storing generated review summaries or downloadable reports
- storing debug artifacts for post-run inspection

Why not now:

- these are useful extensions, not current blockers
- the current product does not need persisted ADK-managed artifacts to complete a review
- introducing artifact persistence would add storage and lifecycle decisions before there is a concrete operational need

## Memory

Decision: defer.

Potential future uses:

- repository-specific preferences learned across runs
- recurring reviewer guidance
- organization-level review heuristics

Why defer:

- there is no concrete cross-run memory use case in the current product
- memory would create additional correctness, privacy, and explainability concerns
- the existing review quality issues are better addressed by prompts, fixtures, and deterministic evals

## Persistent Sessions and Resumability

Decision: keep `InMemorySessionService` unless resumable service-mode runs become necessary.

Why:

- the current review agent is a short-lived one-shot job in CI or CLI mode
- failures are better handled today through reruns than through persisted ADK session recovery
- persistent sessions would require operational decisions around storage, retention, isolation, and cleanup

Revisit this only if we adopt:

- long-running service-mode review workers
- resumable interrupted runs
- restart-from-next-batch recovery requirements across process boundaries

## What This Means For Contributors

When changing the ADK runtime, prefer these defaults:

- keep review execution stateless across runs
- keep SCM orchestration and product policy in Python
- avoid introducing ADK-managed persistence unless the use case is concrete
- treat `auto_create_session=True` as the default session-creation mode for one-shot runners

If a future change needs artifacts, memory, or persistent sessions, document:

- the exact product use case
- why current stateless behavior is insufficient
- what data would be stored
- retention and cleanup expectations
- failure and recovery behavior
