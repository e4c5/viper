## AI Code Review Agent – Implementation Tasks

This document is a **high-level task list** for implementing and refining the AI code review agent.  
For full design details, see the plan in `docs/ai_code_review_agent_280caf7c.plan.md` (referred to as **the Plan** below).

An AI agent executing these tasks should always cross‑check behavior and edge cases against the Plan.

---

## Phase 2: Resolved Issue Tracking and Idempotency

- [ ] **Task 2.1: Auto‑resolve stale comments**
  - [ ] In `runner.run_review`, for each existing comment (`ReviewComment`) returned by `provider.get_existing_review_comments`, compute a fingerprint using current file contents at `head_sha` (reuse `surrounding_content_hash` and `build_fingerprint` from `diff/fingerprint.py`).
  - [ ] When a fingerprint no longer matches any relevant code span in the current head, and `provider.capabilities().resolvable_comments` is `True`, call `provider.resolve_comment(...)` to auto‑resolve that comment.
  - [ ] Ensure behavior matches the “Resolved Issue Tracking” flow in **Phase 2** of the Plan.

- [ ] **Task 2.2: Respect manually resolved comments as an ignore list**
  - [ ] Extend the ignore‑set logic in `runner.py` (currently `_build_ignore_set` and the filtering loop) to track fingerprints/body hashes for **manually resolved** comments separately from unresolved comments, based on `ReviewComment.resolved` (and/or a marker if you adopt one later as described in the Plan).
  - [ ] When posting new findings, skip any whose `(path, content_hash, body_hash or fingerprint)` matches a manually resolved entry, so reviewers are not re‑pestered for issues they intentionally dismissed.
  - [ ] Allow new findings to be posted when code has materially changed and the fingerprint changes, in line with the Plan’s guidance.
  - [ ] Align matching keys with the Plan’s suggested tuple: `(path, content_hash_of_surrounding_lines, issue_code, body_hash/anchor)` where practical.

- [ ] **Task 2.3: Integrate resolved tracking with tests**
  - [ ] Update or add tests to cover the behavior above, aligned with the Plan’s **Phase 2 test plan**:
    - [ ] Extend `tests/providers/test_resolved_tracking.py` to verify resolved vs unresolved behavior is reflected in runner logic (not just the data models).
    - [ ] Extend `tests/providers/test_ignore_fingerprint.py` and `tests/agent/test_ignore_list_integration.py` so they assert:
      - [ ] Manually resolved comments populate the ignore set used by the runner.
      - [ ] Stale comments (no longer matching current code) are auto‑resolved when provider capabilities allow.
      - [ ] New findings against changed code are *not* suppressed incorrectly.

---

## Phase 1: LLM Configuration and Debug Controls

- [ ] **Task 1.1: Implement `LLM_DISABLE_TOOL_CALLS` debug mode**
  - [ ] In `agent/agent.py`, read `disable_tool_calls` from `LLMConfig` (via `get_llm_config()`).
  - [ ] When `disable_tool_calls` is `True`, construct the ADK `Agent` in a mode that **does not use function tools** (e.g. omit tools, or use ADK’s tool‑disable configuration if available).
  - [ ] Ensure the default remains tools‑enabled for normal runs.
  - [ ] Add or update tests (see Plan’s **Phase 1.2** notes) so that when `LLM_DISABLE_TOOL_CALLS` is set, the agent is created in the expected debug configuration.

- [ ] **Task 1.2: Wire LLM timeouts and retries (where supported)**
  - [ ] Where the ADK model or Runner supports it, thread `LLMConfig.timeout_seconds` and `LLMConfig.max_retries` through to the appropriate constructors or configuration objects.
  - [ ] If the current ADK version does not support this cleanly:
    - [ ] Document the limitation in `docs/DEVELOPER_GUIDE.md` with a short note referencing the Plan’s “Timeouts + retry” section.
    - [ ] Optionally, introduce localized guards (e.g. for Ollama) in the HTTP/model layer that use these values, consistent with the Plan.

---

## Phase 4: Provider Capabilities and Suggestions

- [ ] **Task 4.1: Use `ProviderCapabilities` for suggestions**
  - [ ] Extend the posting path in `runner.run_review` so that:
    - [ ] When `provider.capabilities().supports_suggestions` is `True` and a finding carries a suggested patch or code change (see the Plan’s provider‑neutral comment model), the runner fills `InlineComment.suggested_patch` instead of (or in addition to) plain text.
    - [ ] Providers that support suggestion blocks (GitHub, GitLab) convert `InlineComment.suggested_patch` into their platform’s suggested change format, as outlined in the Plan’s **Phase 6.1**.
  - [ ] Update tests in `tests/providers/test_github.py` and `tests/providers/test_gitlab.py` (and any new formatter tests as needed) to assert the correct suggestion payload.

- [ ] **Task 4.2: Guard resolve calls with capabilities**
  - [ ] When implementing auto‑resolve (Task 2.1), ensure the runner only calls `provider.resolve_comment(...)` when `provider.capabilities().resolvable_comments` is `True`.
  - [ ] Add tests for a provider with `resolvable_comments=False` to confirm that `resolve_comment` is not called and that behavior degrades gracefully, matching the Plan’s guidance.

---

## Optional / Future Enhancements (Nice‑to‑Have)

These are lower‑priority items that can be implemented after the core behavior above is stable. They are described in more detail in the Plan.

- [ ] **Optional Task F.1: Queue / debounce webhook events**
  - [ ] Implement a small orchestration layer (likely outside this package, in CI configuration or a thin wrapper) that:
    - [ ] Debounces frequent PR updates (“latest head_sha wins”).
    - [ ] Ensures only one active `run_review` execution per PR/head at a time.
  - [ ] Use the Plan’s “Queue/debounce” architecture notes as the reference for expected behavior.

- [ ] **Optional Task F.2: Stronger repo‑content safety labeling**
  - [ ] Note that the current implementation already truncates large repo files via `truncate_repo_content`.
  - [ ] To fully align with the Plan’s “Repo‑Content Safety Wrapper” section, consider:
    - [ ] Adding an explicit delimiter label (e.g. `--- PROJECT GUIDANCE (untrusted, for context only) ---`) around repo‑sourced context injected into prompts.
    - [ ] Extending or adding tests (e.g. `tests/runner/test_repo_content_safety.py`) to verify that truncation and labeling match the Plan.

---

## How to Use This Document as an AI Agent

- Treat this file as a **roadmap of concrete coding tasks** and the Plan (`docs/ai_code_review_agent_280caf7c.plan.md`) as the **source of truth** for intent, edge cases, and examples.
- For each task:
  - Locate the referenced modules and tests.
  - Re‑read the corresponding section in the Plan (e.g. Phase 2, 4.1, 6.1).
  - Implement changes incrementally, running the relevant test suite (e.g. focused `pytest` paths) after each logical batch of edits.
- When in doubt, prefer behavior that:
  - Keeps the LLM **findings‑only** (runner owns state and posting), and
  - Preserves provider‑agnostic abstractions via `ProviderInterface` and `ProviderCapabilities`, as emphasized in the Plan.

