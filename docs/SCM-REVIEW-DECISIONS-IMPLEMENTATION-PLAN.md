# SCM review decisions — implementation status and gap plan

This document is for **developers**: runner/config status, per-provider implementation for `SCM_REVIEW_DECISION_*`, and reference notes for the GitLab / Bitbucket work (Phases A–C, **complete**). User-facing merge semantics stay in [SCM review decisions and merge blocking](SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md).

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
| `submit_review_decision(...)` | Done | Default raises `NotImplementedError`; GitHub, Gitea, GitLab, Bitbucket Cloud, Bitbucket Server (conditional) override |
| `ProviderCapabilities.supports_review_decisions` | Done | Default `False`; `True` where submission is implemented (Bitbucket Server only when user slug configured) |
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

### 3.3 GitLab — **done (submission)**

- **Quality gate**: **Implemented** — MR discussions (`src/code_review/providers/gitlab.py`).
- **`submit_review_decision`**: **Implemented** — `POST .../approve` (optional `sha`); `REQUEST_CHANGES` first calls `DELETE .../approve` (soft-fail on 404/403/405, so already-not-approved is safe) then posts an MR note + `/submit_review requested_changes` (GitLab may require a pending review for the quick action to apply). The unapprove step prevents the bot from being simultaneously approved and requesting changes after the PR is updated.

### 3.4 Bitbucket Cloud — **done (submission)**

- **Quality gate**: **Partial** — open PR tasks only (unchanged).
- **`submit_review_decision`**: **Implemented** — `POST .../approve` and `.../request-changes`; the opposite endpoint is cleared first (`DELETE /request-changes` before approving; `DELETE /approve` before requesting changes) so state transitions are clean when the PR is re-evaluated; rationale text is posted as a PR-level comment because those endpoints omit `body`.

### 3.5 Bitbucket Server / Data Center — **done (submission, conditional)**

- **Quality gate**: **Implemented** — activities + tasks.
- **`submit_review_decision`**: **Implemented** — `PUT .../participants/{slug}?version=...` with `APPROVED` / `NEEDS_WORK` when **`SCM_BITBUCKET_SERVER_USER_SLUG`** is set; **one retry on HTTP 409** after refetching PR version; `supports_review_decisions` is `True` only in that case.

---

## 4. Plan to close the gap (Phases A–C — **complete**)

GitLab, Bitbucket Cloud, and Bitbucket Server **submission** is implemented (see §3). Phases A–C below are kept as **implementation reference**: what was built, where it lives, and what to re-read when debugging customer installs. They are **not** an active TODO list unless §3 flags a new gap.

Work for any **future** SCM remains **provider-only** (plus tests and docs). Keep `ReviewDecision` as the runner contract; map each SCM’s native model inside the provider.

### Phase A — GitLab (**complete**)

**Delivered**

- **`submit_review_decision`** in `src/code_review/providers/gitlab.py`: `APPROVE` → `POST .../merge_requests/:iid/approve` (optional `sha` from `head_sha`); `REQUEST_CHANGES` → MR note including `/submit_review requested_changes` plus summary text (see `gitlab_note_with_submit_review_requested_changes` in `src/code_review/providers/review_decision_common.py`).
- **`supports_review_decisions=True`** on `GitLabProvider.capabilities()`.
- **Tests:** `tests/providers/test_gitlab.py` — `test_submit_review_decision_approve`, `test_submit_review_decision_request_changes_note`.

**Reference (original risks, still relevant for support)**

- GitLab tier / project settings may affect approvals and review submission; see [SCM review decisions and merge blocking](SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md) and [Merge request approvals API](https://docs.gitlab.com/ee/api/merge_request_approvals.html).

---

### Phase B — Bitbucket Data Center / Server (**complete**)

**Delivered**

- **`submit_review_decision`** in `src/code_review/providers/bitbucket_server.py`: `PUT .../pull-requests/:id/participants/{slug}?version=...` with `APPROVED` / `NEEDS_WORK`; **one retry on HTTP 409** after refetching PR `version` (optimistic locking).
- **`supports_review_decisions`** is `True` only when **`SCM_BITBUCKET_SERVER_USER_SLUG`** is set (non-empty after trim in `SCMConfig`); runner passes it via `get_provider(..., bitbucket_server_user_slug=...)`.
- **Tests:** `tests/providers/test_bitbucket_server.py` — `test_submit_review_decision_needs_work`, `test_submit_review_decision_retries_participant_put_on_409`, `test_submit_review_decision_requires_participant_slug`.

**Reference**

- API surface varies by **Bitbucket Server version**; see provider docstrings and [Bitbucket Server REST API](https://developer.atlassian.com/server/bitbucket/rest/v901/intro/#about).

---

### Phase C — Bitbucket Cloud (**complete**)

**Delivered**

- **`submit_review_decision`** in `src/code_review/providers/bitbucket.py`: `POST .../pullrequests/:id/approve` and `.../request-changes`; Cloud endpoints omit review **body**, so the runner rationale is posted as a PR-level comment via **`post_pr_summary_comment`** using **`effective_review_body(body)`** (`review_decision_common`).
- **`supports_review_decisions=True`** on `BitbucketProvider.capabilities()`.
- **Tests:** `tests/providers/test_bitbucket.py` — `test_submit_review_decision_approve`, `test_submit_review_decision_request_changes`.

**Reference**

- Quality gate remains **partial** (tasks-focused); merge enforcement may depend on workspace **Premium** merge checks — see §3.4 and the merge-blocking doc.

---

## 5. Cross-cutting checklist

Use this when adding a **new** SCM. For GitLab / Bitbucket Cloud / Server, these were covered during Phases A–C.

- [x] Token permissions documented (scopes for approve / review state) — see `docs/CONFIGURATION-REFERENCE.md` and merge-blocking doc.  
- [x] Eligibility: bot user must count as reviewer/approver under customer SCM settings — product doc.  
- [x] Dry run: runner skips HTTP in `_maybe_submit_review_decision`.  
- [x] Observability: `_maybe_submit_review_decision` + provider logs; add debug where needed.  
- [x] No change to `FindingV1` or agent JSON contract.

---

## 6. Optional future work (out of scope for minimal gap fill)

These items were **not** required to finish Phases A–C; they remain possible follow-ups.

- **Extend `ReviewDecision`** or capabilities if an SCM needs a third state (e.g. COMMENT-only review).  
- **GitLab-only:** If REST cannot request changes on CE, consider GraphQL client module shared with future features.  
- **Bitbucket Cloud quality gate:** Enrich counts from inline comments if API gains resolution state.  
- **Integration tests:** Live GitLab / BB against dockerized SCM (heavy maintenance).

---

## 7. Related files (quick index)

| File | Role |
|------|------|
| `src/code_review/runner.py` | `_quality_gate_*`, `_maybe_submit_review_decision` |
| `src/code_review/config.py` | `SCMConfig` including `review_decision_*`, `bitbucket_server_user_slug` |
| `src/code_review/providers/base.py` | `ReviewDecision`, `submit_review_decision`, `get_unresolved_review_items_for_quality_gate` |
| `src/code_review/providers/__init__.py` | `get_provider(..., *, bitbucket_server_user_slug=...)` |
| `src/code_review/providers/review_decision_common.py` | Shared `effective_review_body`, GitHub-style JSON, GitLab note helper |
| `src/code_review/providers/github.py` | Reference `submit_review_decision` |
| `src/code_review/providers/gitea.py` | `submit_review_decision` + HTTP soft-fail on unsupported methods |
| `src/code_review/providers/gitlab.py` | Quality gate + **`submit_review_decision`** (approve + MR note / quick action) |
| `src/code_review/providers/bitbucket.py` | Partial task gate + **`submit_review_decision`** (approve / request-changes + summary comment) |
| `src/code_review/providers/bitbucket_server.py` | Quality gate + **`submit_review_decision`** (participant PUT, conditional on user slug) |
| `tests/test_runner.py` | Decision orchestration |
| `tests/providers/test_github.py`, `test_gitea.py` | GitHub/Gitea submission |
| `tests/providers/test_gitlab.py`, `test_bitbucket.py`, `test_bitbucket_server.py` | GitLab / Bitbucket Cloud / Server submission |
| `tests/providers/test_review_decision_common.py` | Shared review-decision helpers |
