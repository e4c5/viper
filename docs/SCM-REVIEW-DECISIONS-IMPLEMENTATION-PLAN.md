# SCM review decisions — implementation plan

This document is for **developers**: what exists today for `SCM_REVIEW_DECISION_*`, what was validated against the current code base, and the **planned work** to re-evaluate approve vs needs-work when review threads change without a full agent run. User-facing merge semantics stay in [SCM review decisions and merge blocking](SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md).

Validated against the repository state on **2026-03-22**.

Follow **single responsibility**, established patterns, and **reuse** shared infrastructure (config, model factory, logging, orchestration) without merging unrelated prompts or tools into one agent.

---

## 0. Validation snapshot

### 0.1 Confirmed in the current code base

- Review-decision config already exists in `SCMConfig` (`review_decision_enabled`, `review_decision_high_threshold`, `review_decision_medium_threshold`) and is also exposed as CLI overrides in [`src/code_review/__main__.py`](/home/raditha/workspace/python/code-review/viper/src/code_review/__main__.py).
- Full-review submission already works through `_quality_gate_high_medium_counts`, `_compute_review_decision_from_counts`, and `_maybe_submit_review_decision` in [`src/code_review/runner.py`](/home/raditha/workspace/python/code-review/viper/src/code_review/runner.py).
- The current submission call site is `ReviewOrchestrator._post_findings_and_summary(...)`, after inline posting and after the omit-marker PR summary path used by Bitbucket providers.
- Provider support for `submit_review_decision(...)` exists for GitHub, Gitea, GitLab, Bitbucket Cloud, and Bitbucket Server/DC (Bitbucket Server/DC only when `SCM_BITBUCKET_SERVER_USER_SLUG` is configured).
- Provider-specific quality-gate aggregation exists today:
  - GitHub: unresolved, non-outdated review threads via GraphQL.
  - Gitea: unresolved review comments when `resolved` is exposed.
  - GitLab: unresolved discussions (`resolved=false`) at thread level.
  - Bitbucket Cloud: open PR tasks only.
  - Bitbucket Server/DC: unresolved inline comments plus open PR tasks.
- Tests already cover the implemented baseline in [`tests/test_runner.py`](/home/raditha/workspace/python/code-review/viper/tests/test_runner.py), [`tests/config/test_config.py`](/home/raditha/workspace/python/code-review/viper/tests/config/test_config.py), [`tests/providers/test_review_decision_common.py`](/home/raditha/workspace/python/code-review/viper/tests/providers/test_review_decision_common.py), and provider-specific test modules.

### 0.2 Confirmed gaps

- There is **no** review-decision-only mode yet.
- There is **no** provider-neutral event/context object for comment-driven re-evaluation.
- There is **no** API to ask whether the bot is **currently blocking** the PR/MR.
- There is **no** reply-dismissal agent, reply classification schema, or runner path for classifying user replies.
- The bundled Jenkins pipeline currently triggers full runs only for push-style PR actions (`opened`, `reopened`, `synchronize`, `synchronized`, or Bitbucket `pr:*`), not comment/discussion-only changes.

### 0.3 Corrections to earlier wording

- GitHub quality-gate behavior on GraphQL failure is **not** a REST fallback. The current code logs a warning and returns `[]`, so only the current run's `to_post` findings contribute in that case.
- The runner is now **ReviewOrchestrator-based**. `run_review(...)` is a thin wrapper; future work should hook into the orchestrator, not duplicate logic beside it.
- Any future review-decision-only path must avoid accidentally reusing `_post_findings_and_summary(...)` as-is, because that method also handles inline posting, stale-resolution, and Bitbucket omit-marker PR summary comments.

---

## 1. Baseline: runner and config (done)

| Area | Location | Notes |
|------|----------|-------|
| Env config | `SCMConfig` in `src/code_review/config.py` | `SCM_REVIEW_DECISION_ENABLED`, `SCM_REVIEW_DECISION_HIGH_THRESHOLD`, `SCM_REVIEW_DECISION_MEDIUM_THRESHOLD`, `SCM_BITBUCKET_SERVER_USER_SLUG` |
| CLI overrides | `review(...)` in `src/code_review/__main__.py` | `--review-decision-enabled`, `--review-decision-high-threshold`, `--review-decision-medium-threshold` |
| Orchestration entry | `run_review(...)` and `ReviewOrchestrator` in `src/code_review/runner.py` | `run_review(...)` passes per-run overrides into the orchestrator |
| Threshold logic | `_compute_review_decision_from_counts` in `src/code_review/runner.py` | Returns `REQUEST_CHANGES` or `APPROVE` |
| Aggregated counts | `_quality_gate_high_medium_counts` in `src/code_review/runner.py` | Combines provider unresolved items with this run's `to_post`, deduped |
| Submit | `_maybe_submit_review_decision` in `src/code_review/runner.py` | Checks config, provider capability, and `dry_run` |
| Call site | `ReviewOrchestrator._post_findings_and_summary` in `src/code_review/runner.py` | Runs after inline posting and after omit-marker summary handling |
| Supporting review-decision text | `_optional_quality_gate_summary_suffix` in `src/code_review/runner.py` | Recomputes the same counts/decision for PR summary text |
| Tests | `tests/test_runner.py`, `tests/config/test_config.py`, `tests/providers/test_*` | Baseline behavior is already well covered |

Adding a **new** SCM still means: implement `submit_review_decision`, set `supports_review_decisions=True` where applicable, implement or inherit `get_unresolved_review_items_for_quality_gate`, and add provider tests.

---

## 2. Baseline: interface (done)

| Item | Location | Notes |
|------|----------|-------|
| `ReviewDecision` | `Literal["APPROVE", "REQUEST_CHANGES"]` in `src/code_review/providers/base.py` | Current contract |
| `submit_review_decision(...)` | `ProviderInterface` in `src/code_review/providers/base.py` | Default raises `NotImplementedError` |
| `ProviderCapabilities.supports_review_decisions` | `src/code_review/providers/base.py` + provider overrides | Bitbucket Server/DC is conditional on configured user slug |
| `get_unresolved_review_items_for_quality_gate(...)` | `ProviderInterface` in `src/code_review/providers/base.py` | Default maps unresolved comments into `UnresolvedReviewItem` |
| `UnresolvedReviewItem` | `src/code_review/providers/base.py` | Existing normalized shape for quality-gate aggregation |

No interface exists yet for:

- current bot blocking state
- current bot identity
- comment-webhook event context
- thread/reply classification input

Those should be added deliberately instead of being inferred ad hoc inside provider methods.

---

## 3. Baseline: per-provider behavior (done)

| Provider | Submission | Quality gate (open high/medium signals) | Validation notes |
|----------|------------|------------------------------------------|------------------|
| **GitHub** | `POST .../pulls/{id}/reviews` | GraphQL `reviewThreads`: unresolved and non-outdated | On GraphQL failure, current code returns `[]`; there is no REST unresolved fallback |
| **Gitea** | GitHub-style review submission; soft-fails on 404/405/501 | Default unresolved-comment path from `get_existing_review_comments` and `resolved` when exposed | No thread/outdated model in-repo |
| **GitLab** | `POST .../approve`; `REQUEST_CHANGES` clears approve then posts MR note with `/submit_review requested_changes` | Unresolved MR discussions with severity inferred from diff notes in the thread | Closest existing fit for reply-thread work |
| **Bitbucket Cloud** | approve / request-changes + DELETE opposite state first; then PR summary comment | Open PR tasks only | Still tasks-first; inline-thread resolution is not modeled today |
| **Bitbucket Server/DC** | `PUT .../participants/{slug}` with `APPROVED` / `NEEDS_WORK` | Unresolved inline comments plus open PR tasks | Requires `SCM_BITBUCKET_SERVER_USER_SLUG` |

Tests: `tests/providers/test_github.py`, `tests/providers/test_gitea.py`, `tests/providers/test_gitlab.py`, `tests/providers/test_bitbucket.py`, `tests/providers/test_bitbucket_server.py`, `tests/providers/test_review_decision_common.py`.

---

## 4. Problem confirmed by validation

Today, review-decision recomputation happens only at the **end of a full review run**. That is fine when authors push a new commit and CI reruns the full pipeline. It is not enough when the PR state changes through **discussion activity only**.

Confirmed gaps:

1. **No comment-driven trigger path**. The bundled Jenkins pipeline only allows push-style PR actions. Resolving a thread, deleting a comment, or replying to a finding does not currently invoke any decision-only logic.
2. **No decision-only orchestration path**. The code only has the full review flow. There is no runner branch that skips the code-review agent and only recomputes the gate.
3. **No “only if bot is blocking” optimization**. There is no provider API to ask whether the bot currently has an active blocking state on the PR/MR.
4. **No transition handling for accepted replies, deleted comments, or outdated threads**. Today there is no dedicated decision-only path for the cases where a blocking review item should stop counting because:
   - a human reply is accepted by the reply-dismissal flow
   - the underlying review comment or thread is deleted by someone with sufficient access
   - the SCM marks the thread outdated / no longer applicable
   In all three cases, the required follow-up is the same: recompute the gate from current SCM state and decide whether the bot should transition back to `APPROVE`.
5. **Head SHA and side-effect handling are underspecified for comment-only runs**. A decision-only path will need a safe answer for:
   - missing or stale `head_sha`
   - whether omit-marker PR summary comments should be skipped
   - whether idempotency for comment-driven runs is SHA-based, comment-id-based, deletion-event-based, or a combination

---

## 5. Plan improvements to adopt before implementation

### 5.1 Extract shared review-decision computation

The current runner computes quality-gate counts in more than one place (`_optional_quality_gate_summary_suffix` and `_maybe_submit_review_decision`). Before adding more modes, extract a shared helper that returns:

- aggregated `high_count`
- aggregated `medium_count`
- final `decision`
- human-readable `reason`

This reduces drift between summary text, decision-only mode, and full-review mode.

### 5.2 Add a provider-neutral event context model

Do **not** pass a growing pile of loosely related CLI flags. Add a small typed event model first, for example:

```text
ReviewDecisionEventContext
- event_name
- event_action
- event_kind = reply_added | comment_deleted | thread_outdated | thread_resolved | scheduled | other
- comment_id
- thread_id
- actor_login / actor_id
- head_sha
- source = full_review | webhook_comment | webhook_thread | scheduled
```

This gives the orchestrator one stable input surface for comment-webhook re-evaluation and keeps provider-specific payload parsing outside the core runner.

### 5.3 Add a bot-identity abstraction before Phase C/D

Both “only if bot is blocking” and reply-dismissal classification need a reliable answer to “which comments/reviews belong to the bot?”. Add this explicitly instead of scattering username matching logic through providers.

Recommended shape:

- `ProviderInterface.get_current_actor_identity(...)` or equivalent
- optional config override for servers where the API identity is ambiguous
- shared helper for “is bot authored?” checks

### 5.4 Keep review-decision-only mode side-effect-light

Do **not** reuse `_post_findings_and_summary(...)` for decision-only runs. That method currently:

- auto-resolves stale comments
- posts inline comments
- posts omit-marker PR summary comments for Bitbucket
- submits the final review decision

A decision-only path should normally do only:

1. gather context
2. decide whether this is a pure state-change recomputation (`comment_deleted`, `thread_outdated`, `thread_resolved`) or a reply-classification case
3. recompute counts
4. submit or skip the review decision
5. optionally post a reply-classification follow-up when the verdict is `disagreed`

This avoids extra noise on comment-only events.

### 5.5 Prioritize providers in the order their data models support

Recommended delivery order:

1. **GitHub**
2. **GitLab**
3. **Bitbucket Server/DC**
4. **Gitea**
5. **Bitbucket Cloud**

Reasoning:

- GitHub and GitLab already have thread-level gate semantics in-repo.
- Bitbucket Server/DC has enough activity/task data to be workable.
- Gitea may need API capability verification for reply/thread context.
- Bitbucket Cloud is still task-centric, so reply-dismissal may remain out of scope or partial at first.

### 5.6 Include cross-doc cleanup in the implementation work

At least one companion doc should be rechecked during rollout:

- `README.md` currently describes GitHub quality-gate behavior more optimistically than the code path that actually returns `[]` on GraphQL failure.

This plan should stay the source of truth for implementation detail, but the public docs should not drift.

---

## 6. Technical plan (phased)

### Phase A — Extract shared gate computation and add testable building blocks

- Extract a shared helper in `runner.py` for aggregated counts + decision + reason text.
- Keep `_maybe_submit_review_decision(...)` as a thin side-effect wrapper around that helper.
- Reuse the same helper from `_optional_quality_gate_summary_suffix(...)`.
- Add tests for:
  - thresholds
  - dedupe behavior
  - provider unresolved-item failures
  - shared reason string generation

This phase is low risk and should happen before any new mode is introduced.

### Phase B — Review-decision-only mode (runner + CLI)

- Add a mode (CLI flag and env), for example:
  - `--review-decision-only`
  - `CODE_REVIEW_REVIEW_DECISION_ONLY=true`
- Implement it in `ReviewOrchestrator`, not as a separate parallel code path outside the orchestrator.
- The mode should:
  - skip the code review agent
  - skip inline posting
  - skip stale comment resolution
  - skip Bitbucket omit-marker PR summary posting
  - still compute counts and call review-decision submission
  - optionally invoke reply-dismissal classification in a later phase
- Decide head SHA behavior explicitly:
  - use event-provided `head_sha` when present
  - otherwise fetch the current PR/MR head SHA from the provider before submission

**Tests**

- CLI override wiring
- orchestrator decision-only path
- dry-run behavior
- missing-head-sha behavior
- “no provider support” behavior

### Phase C — Event/context plumbing and non-push triggers

- Add a provider-neutral `ReviewDecisionEventContext` model.
- Document and optionally wire webhook/CI filters so comment and discussion events can invoke Phase B.
- Update repository docs and examples:
  - `docker/jenkins/Jenkinsfile`
  - Jenkins setup docs
  - GitHub Actions docs where applicable
- Keep existing push-triggered full-review behavior unchanged.

Notes:

- One event per new comment is the common case. The event should ideally identify the specific `comment_id` that triggered the run.
- Delete/outdated/resolved events should be modeled as the same class of trigger as reply events: they should invoke decision-only recomputation even though they do not require LLM classification.
- Scheduled or batch runs can omit `comment_id` and fall back to SCM state inspection plus “latest relevant human reply on each candidate thread” where classification is needed.

### Phase D — “Only if bot is blocking” provider API

- Add a tri-state interface, for example:
  - `BLOCKING`
  - `NOT_BLOCKING`
  - `UNKNOWN`
- Implement provider-specific adapters:
  - **GitHub / Gitea**: latest bot-authored review state on the PR
  - **GitLab**: approval/request-changes state visible to the bot identity
  - **Bitbucket Cloud**: approve vs request-changes state for the bot
  - **Bitbucket Server/DC**: participant `NEEDS_WORK` vs `APPROVED`
- In review-decision-only mode:
  - skip recomputation when state is `NOT_BLOCKING` and the event does not imply new blocking evidence
  - recompute when state is `BLOCKING`
  - recompute on `UNKNOWN` as the safe default

This should land before reply-dismissal so comment-driven runs stay cheap when the bot is not currently blocking anything.

### Phase E — Reply, delete, and outdated-thread transitions

Use a **separate agent** from the code review agent.

#### E.1 Data and provider fetchers

- Extend provider data fetchers so the runner can assemble:
  - the original bot review comment
  - subsequent human replies
  - author metadata
  - timestamps / ordering
  - enough SCM state to tell whether a previously blocking item has been deleted, resolved, or marked outdated
- Recommended first implementations:
  - **GitHub**: thread comments from GraphQL
  - **GitLab**: discussion notes
  - **Bitbucket Server/DC**: activities/comments if enough thread data exists
  - **Gitea**: only after verifying thread/reply APIs
  - **Bitbucket Cloud**: keep tasks-first until richer reply semantics are proven

#### E.2 Reply-dismissal agent

- Add a new module, e.g. `src/code_review/agent/reply_dismissal_agent.py`.
- Use the same model wiring defaults as the main agent unless optional overrides are later added.
- Tools: **none**.
- Output contract:

```json
{
  "verdict": "agreed" | "disagreed",
  "reply_text": "<required when verdict is disagreed>"
}
```

- Validate with Pydantic before the runner acts on it.

#### E.3 Gate integration

- On a comment webhook:
  - map the event to one thread and one human reply
  - run classification once
  - if `agreed`, exclude that thread from the gate counts for this recomputation
  - if `disagreed`, keep the thread in counts and pass `reply_text` to the SCM posting layer
- On a delete / outdated / resolved webhook:
  - do **not** run reply classification
  - recompute the gate from current SCM state after the item disappears from, or is excluded by, the provider's unresolved-item view
  - if counts now fall below threshold, transition the bot back to `APPROVE`
- On batch/scheduled runs:
  - classify only candidate threads that have new human replies after the bot comment
  - treat deleted or outdated items as ordinary state removals from the unresolved set
  - cap the number of LLM calls per run

#### E.4 Operational behavior

- Ignore bot-authored comments for dismissal attempts.
- Log the verdict for auditability.
- Log delete / outdated / resolved recomputation paths distinctly from reply-classification paths.
- In dry-run mode, log the would-be `reply_text` instead of posting it.
- Keep prompt stance pragmatic rather than adversarial.

### Phase F — Docs, observability, and cleanup

- Update [SCM review decisions and merge blocking](SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md).
- Update `README.md` so provider semantics match the real implementation.
- Log clearly when a run is:
  - full review
  - review-decision-only
  - skipped because bot is not blocking
  - skipped because provider state is unsupported/unknown
- Add metrics or structured logs for:
  - decision-only runs
  - blocking-state skips
  - reply-dismissal verdict counts
  - provider/API fallbacks

---

## 7. Related files (validated index)

| File | Role |
|------|------|
| `src/code_review/runner.py` | review-decision aggregation, submission, and orchestrator flow |
| `src/code_review/__main__.py` | CLI overrides; future home of `--review-decision-only` |
| `src/code_review/config.py` | review-decision config; likely future env for decision-only mode and optional bot identity overrides |
| `src/code_review/providers/base.py` | `ReviewDecision`, `UnresolvedReviewItem`, future blocking-state / identity / event interfaces |
| `src/code_review/providers/review_decision_common.py` | shared helpers for review-decision submission |
| `src/code_review/providers/github.py` | thread-based gate; strong candidate for first reply-dismissal implementation |
| `src/code_review/providers/gitlab.py` | discussion-based gate; strong candidate for first reply-dismissal implementation |
| `src/code_review/providers/gitea.py` | comment-based gate; thread support must be validated before dismissal work |
| `src/code_review/providers/bitbucket.py` | task-first gate; likely partial/late reply-dismissal support |
| `src/code_review/providers/bitbucket_server.py` | inline-comment + task gate; needs participant-state reuse for blocking checks |
| `src/code_review/agent/agent.py` | code review agent only; do not merge reply-dismissal logic into this prompt |
| `src/code_review/agent/reply_dismissal_agent.py` (planned) | reply-dismissal agent |
| `docker/jenkins/Jenkinsfile` | current PR trigger filtering; must be updated for comment-only decision runs |
| `tests/test_runner.py` | review-decision orchestration tests |
| `tests/config/test_config.py` | config and CLI override behavior |
| `tests/providers/test_*.py` | per-provider review-decision and gate semantics |

---

## 8. Ordered checklist

1. [ ] Extract a shared runner helper for `high_count`, `medium_count`, `decision`, and `reason`, and reuse it from both `_maybe_submit_review_decision(...)` and `_optional_quality_gate_summary_suffix(...)`.
2. [ ] Add tests for the shared helper so future review modes do not drift in threshold or dedupe behavior.
3. [ ] Introduce a provider-neutral `ReviewDecisionEventContext` model and thread it through `ReviewOrchestrator`.
4. [ ] Add a review-decision-only mode in CLI and config, implemented inside the orchestrator and intentionally skipping agent execution, inline posting, stale-resolution, and omit-marker PR summary comments.
5. [ ] Define how decision-only runs obtain `head_sha` when the triggering event does not provide one, and implement provider support if a fetch is required.
6. [ ] Add a bot-identity abstraction to providers so later work can reliably distinguish bot comments/reviews from human replies.
7. [ ] Add a tri-state provider API for “is the bot currently blocking this PR/MR?” and use it to short-circuit decision-only runs when safe.
8. [ ] Update Jenkins and other trigger docs/examples so comment/discussion events can invoke review-decision-only runs.
9. [ ] Extend provider event/context handling so review-item deletion, thread resolution, and thread outdated transitions also trigger decision-only recomputation.
10. [ ] Implement provider fetchers for reply-thread context and deleted/outdated-item detection, starting with GitHub and GitLab.
11. [ ] Add the reply-dismissal agent, schema validation, and runner integration for single-reply classification.
12. [ ] Add optional SCM follow-up posting for `disagreed` verdicts, with dry-run logging and rate limits.
13. [ ] Update companion docs and observability so the new behavior, provider limitations, and fallback paths are visible and accurate.

---

## 9. Optional follow-ups

- Extend `ReviewDecision` or provider capabilities if a future SCM needs a third native state beyond approve / request-changes.
- Revisit Bitbucket Cloud if the API later exposes reliable reply-thread resolution semantics.
- Consider heavier live SCM integration tests only after the provider contracts stabilize.
