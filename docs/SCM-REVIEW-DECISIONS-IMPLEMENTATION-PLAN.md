# SCM review decisions — implementation status and gap plan

This document is for **developers**: what is already wired in code for `SCM_REVIEW_DECISION_*`, what is missing per provider, and a practical order of work. User-facing merge semantics stay in [SCM review decisions and merge blocking](SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md).

---

## 1. Runner and config (complete)

| Area | Status | Location |
|------|--------|----------|
| Env / CLI overrides | Done | `SCMConfig` in `src/code_review/config.py`; `review` command in `src/code_review/__main__.py` |
| Threshold logic | Done | `_compute_review_decision_from_counts` in `src/code_review/runner.py` |
| Aggregated counts | Done | `_quality_gate_high_medium_counts` → `provider.get_unresolved_review_items_for_quality_gate` + `to_post` findings |
| Submit gate | Done | `_maybe_submit_review_decision`: checks `review_decision_enabled`, `capabilities().supports_review_decisions`, `dry_run` |
| Call site | Done | End of `_post_inline_comments` in `src/code_review/runner.py` (after inline post + optional run marker) |
| Tests (runner) | Done | `tests/test_runner.py` (`test_run_review_*review_decision*` mocks) |

No runner changes are **required** to add a new SCM: implement `submit_review_decision`, set `supports_review_decisions=True`, and add provider tests.

---

## 2. Interface (complete)

| Item | Status | Location |
|------|--------|----------|
| `ReviewDecision` | Done | `Literal["APPROVE", "REQUEST_CHANGES"]` in `src/code_review/providers/base.py` |
| `submit_review_decision(...)` | Done | Default raises `NotImplementedError`; GitHub/Gitea override |
| `ProviderCapabilities.supports_review_decisions` | Done | Default `False`; GitHub/Gitea `True` |
| `get_unresolved_review_items_for_quality_gate` | Done | Default uses `get_existing_review_comments`; overrides for thread/task semantics |

---

## 3. Per-provider status

### 3.1 GitHub — **done**

- **`submit_review_decision`**: `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` with `event` + optional `commit_id` (`src/code_review/providers/github.py`).
- **Quality gate**: GraphQL `reviewThreads` → `UnresolvedReviewItem` (unresolved, non-outdated); on GraphQL failure returns `[]` (counts rely on this run’s `to_post` only).
- **Tests**: `tests/providers/test_github.py` (`test_submit_review_decision`, capabilities).

### 3.2 Gitea — **done**

- **`submit_review_decision`**: Same shape as GitHub; `404`/`405`/`501` logged and swallowed (`src/code_review/providers/gitea.py`).
- **Quality gate**: Default path from `get_existing_review_comments` (`resolved=false` when API exposes it).
- **Tests**: `tests/providers/test_gitea.py`.

### 3.3 GitLab — **gap (submission only)**

- **Quality gate**: **Implemented** — `get_unresolved_review_items_for_quality_gate` walks MR discussions, unresolved threads, severity from diff notes (`src/code_review/providers/gitlab.py`).
- **`submit_review_decision`**: **Not implemented** — inherits default; `supports_review_decisions` remains `False`, so the runner logs *Skipping review decision submission* with the computed decision.
- **Posting comments**: Uses merge request discussions API (separate from approvals).

### 3.4 Bitbucket Cloud — **gap (submission only)**

- **Quality gate**: **Partial** — only **open PR tasks** count (`get_unresolved_review_items_for_quality_gate`); inline comment severity does not feed the gate (API limitation documented in README).
- **`submit_review_decision`**: **Not implemented**; `supports_review_decisions` is `False`.

### 3.5 Bitbucket Server / Data Center — **gap (submission only)**

- **Quality gate**: **Implemented** — non-resolved comments from activities + open tasks (`src/code_review/providers/bitbucket_server.py`).
- **`submit_review_decision`**: **Not implemented**; `supports_review_decisions` is `False`.

---

## 4. Plan to close the gap

Work is **provider-only** (plus tests and docs touch-up). Keep `ReviewDecision` as the runner contract; map each SCM’s native model inside the provider.

### Phase A — GitLab (recommended first)

**Why first:** Thread-level quality gate already matches GitLab semantics; many installations expect MR-level approve / request changes.

1. **Spike (half day–1 day)**  
   - Confirm target GitLab version (CE vs EE, min version for “request changes” reviews).  
   - **Approve:** `POST /projects/:id/merge_requests/:merge_request_iid/approve` ([Merge request approvals API](https://docs.gitlab.com/ee/api/merge_request_approvals.html)) — project id is URL-encoded path or numeric id; provider already has `_path(owner, repo, ...)`.  
   - **Request changes:** Identify supported API (often **GraphQL** `mergeRequestReviewSubmit` / draft-note publish paths, or version-specific REST). Document the chosen call in code comments.

2. **Implement `submit_review_decision` in `gitlab.py`**  
   - `APPROVE` → approve endpoint; optional `sha` = `head_sha` when supported.  
   - `REQUEST_CHANGES` → chosen review API with summary `body`.  
   - Handle **401/403** (token not an eligible approver / insufficient scope) with clear logs.  
   - If “request changes” is unavailable on older CE, either: keep capability false until min version, or implement **APPROVE-only** behind an extra env flag (only if product explicitly needs it).

3. **Set `supports_review_decisions=True`** once both outcomes work on the supported matrix (or document partial support).

4. **Tests** — `tests/providers/test_gitlab.py`: mock `httpx` / client, assert payloads and URLs for approve + request-changes paths.

5. **Docs** — Update [SCM review decisions and merge blocking](SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md) §2 table and GitLab §3.3 “Viper” row; note GitLab tier/settings for merge blocking (unchanged product facts).

**Risks:** Approval endpoints and “request changes” may require **Premium** features or specific project settings; CI matrix may need a GitLab version container for integration smoke tests (optional).

---

### Phase B — Bitbucket Data Center / Server

**Why second:** Same `bitbucket_server` provider as Jenkins DC docs; merge checks often depend on participant state (“needs work”).

1. **Spike**  
   - From [Bitbucket Server REST API](https://developer.atlassian.com/server/bitbucket/rest/v901/intro/#about) (match your server version): find how to set the **current user’s** review state on a pull request (e.g. approve vs needs-work / unapprove).  
   - Map `APPROVE` and `REQUEST_CHANGES` to those states (names may be `APPROVED` / `NEEDS_WORK` in API enums).

2. **Implement in `bitbucket_server.py`**  
   - Use existing `_path` / `_post` / `_put` patterns.  
   - Respect **409/404** (wrong PR version, permission): log and optionally no-op like Gitea for known unsupported cases.

3. **`supports_review_decisions=True`** when both paths verified.

4. **Tests** — `tests/providers/test_bitbucket_server.py` with mocked REST.

5. **Docs** — Update merge-blocking doc §2 and §3.5.

**Risks:** API varies by **Bitbucket Server version**; document minimum version in provider docstring.

---

### Phase C — Bitbucket Cloud

**Why last:** Quality gate is task-heavy; merge enforcement often needs **Premium** merge checks — still valuable for teams that use default reviewers + “no changes requested.”

1. **Spike**  
   - [Bitbucket Cloud REST API](https://developer.atlassian.com/cloud/bitbucket/rest/intro/) — endpoints for **pull request approvals** or **participant** state (workspace/repo/PR id).  
   - Confirm whether a single integration user can represent “bot approve / request changes” for merge checks.

2. **Implement in `bitbucket.py`**  
   - Map to Cloud’s model (may differ from Server).

3. **Tests** — `tests/providers/test_bitbucket.py`.

4. **Docs** — Update merge-blocking doc §2 and §3.4; keep task-only quality gate caveat visible.

**Risks:** Cloud API may not expose a perfect analogue to GitHub’s single review submission; may require **two calls** (e.g. decline + comment) — encapsulate inside `submit_review_decision`.

---

## 5. Cross-cutting checklist (each phase)

- [ ] Token permissions documented (scopes for approve / review state).  
- [ ] Eligibility: bot user must count as reviewer/approver under customer SCM settings.  
- [ ] Dry run: runner already avoids HTTP; no change.  
- [ ] Observability: existing log lines in `_maybe_submit_review_decision` suffice; add provider debug logs if needed.  
- [ ] No change to `FindingV1` or agent JSON contract.

---

## 6. Optional future work (out of scope for minimal gap fill)

- **Extend `ReviewDecision`** or capabilities if an SCM needs a third state (e.g. COMMENT-only review).  
- **GitLab-only:** If REST cannot request changes on CE, consider GraphQL client module shared with future features.  
- **Bitbucket Cloud quality gate:** Enrich counts from inline comments if API gains resolution state.  
- **Integration tests:** Live GitLab / BB against dockerized SCM (heavy maintenance).

---

## 7. Related files (quick index)

| File | Role |
|------|------|
| `src/code_review/runner.py` | `_quality_gate_*`, `_maybe_submit_review_decision` |
| `src/code_review/providers/base.py` | `ReviewDecision`, `submit_review_decision`, `get_unresolved_review_items_for_quality_gate` |
| `src/code_review/providers/github.py` | Reference implementation |
| `src/code_review/providers/gitea.py` | Reference implementation + HTTP soft-fail |
| `src/code_review/providers/gitlab.py` | Quality gate done; submission TODO |
| `src/code_review/providers/bitbucket.py` | Tasks-only gate; submission TODO |
| `src/code_review/providers/bitbucket_server.py` | Gate done; submission TODO |
| `tests/test_runner.py` | Decision orchestration |
| `tests/providers/test_github.py`, `test_gitea.py` | Provider submission patterns |
