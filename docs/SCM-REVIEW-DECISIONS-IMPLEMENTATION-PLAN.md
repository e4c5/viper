# SCM review decisions — implementation plan

This document is for **developers**: what exists today for `SCM_REVIEW_DECISION_*`, where it lives, and the **planned work** to re-evaluate approve vs needs-work when review threads change without a full agent run. User-facing merge semantics stay in [SCM review decisions and merge blocking](SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md).


It is very important that we follow the Single Responsibility principal when building this. We should rely on 
established design patterns where possible and always strive to reuse code as much as possible.

---

## 1. Baseline: runner and config (done)

| Area | Location |
|------|----------|
| Env / CLI | `SCMConfig` in `src/code_review/config.py`; `review` in `src/code_review/__main__.py` |
| Threshold logic | `_compute_review_decision_from_counts` in `src/code_review/runner.py` |
| Aggregated counts | `_quality_gate_high_medium_counts` → `provider.get_unresolved_review_items_for_quality_gate` + `to_post` findings |
| Submit | `_maybe_submit_review_decision` (checks `review_decision_enabled`, `capabilities().supports_review_decisions`, `dry_run`) |
| Call site | End of `_post_findings_and_summary` in `src/code_review/runner.py` (after inline posts + optional omit-marker summary) |
| Tests | `tests/test_runner.py` (`test_run_review_*review_decision*` mocks) |

Adding a **new** SCM still means: implement `submit_review_decision`, set `supports_review_decisions=True` where applicable, implement or inherit `get_unresolved_review_items_for_quality_gate`, add tests.

---

## 2. Baseline: interface (done)

| Item | Location |
|------|----------|
| `ReviewDecision` | `Literal["APPROVE", "REQUEST_CHANGES"]` in `src/code_review/providers/base.py` |
| `submit_review_decision(...)` | Default raises `NotImplementedError`; GitHub, Gitea, GitLab, Bitbucket Cloud, Bitbucket Server (conditional) override |
| `ProviderCapabilities.supports_review_decisions` | `True` only where submission is implemented (Bitbucket Server only when user slug configured) |
| `get_unresolved_review_items_for_quality_gate` | Default uses `get_existing_review_comments`; overrides for thread/task semantics |

---

## 3. Baseline: per-provider behavior (done)

| Provider | Submission | Quality gate (open high/medium signals) |
|----------|------------|----------------------------------------|
| **GitHub** | `POST .../pulls/{id}/reviews` | GraphQL `reviewThreads`: unresolved **and** non-outdated; GraphQL failure → `[]` (counts rely on this run’s `to_post` only) |
| **Gitea** | GitHub-style; soft-fail if unsupported | Default path from `get_existing_review_comments` / `resolved` when API exposes it — **no** GraphQL thread/outdated model in-repo |
| **GitLab** | `POST .../approve`; `REQUEST_CHANGES` clears approve then MR note + `/submit_review requested_changes` | MR discussions with `resolved: false` |
| **Bitbucket Cloud** | approve / request-changes + DELETE opposite state first | **Partial** — open PR tasks (see README table) |
| **Bitbucket Server** | `PUT .../participants/{slug}` when `SCM_BITBUCKET_SERVER_USER_SLUG` set | Activities + open tasks |

Tests: `tests/providers/test_github.py`, `test_gitea.py`, `test_gitlab.py`, `test_bitbucket.py`, `test_bitbucket_server.py`, `test_review_decision_common.py`.

---

## 4. Problem: transitions back to APPROVE without a new push

Today, `_quality_gate_high_medium_counts` and `_maybe_submit_review_decision` run only at the **end of a full review** (after the agent and posting). That is sufficient when authors **push** (new `head_sha`, Jenkins/GitHub Actions typically fire again): GitHub already excludes **outdated** threads; resolved discussions and deleted comments disappear from APIs on the next fetch.

Gaps:

1. **Triggers** — Bundled Jenkins allows PR actions such as `opened` / `reopened` / `synchronize` (and Bitbucket `pr:*`), not **comment-only** events. If someone **deletes** a comment, **resolves** a thread, or **replies** without pushing, the full runner does not run, so the bot may stay on REQUEST_CHANGES / needs-work even when counts would now pass.
2. **“Replied to”** — Not implemented yet; **planned:** LLM classification of replies that dispute/dismiss the finding (see §5.1, Phase D). Until then, unresolved threads with replies still count unless resolved or outdated.
3. **Optimization** — There is no API-backed check that the bot is **currently blocking** the PR before doing expensive or noisy work; the user requirement is to **only** recompute when the PR is already in rejected / needs-work (from the bot’s perspective).

---

## 5. Product decisions

### 5.1 “Replied to” — LLM classification (target behavior)

For threads that still appear **open** in the SCM (unresolved, non-outdated) but have **user replies** after the review comment, we do **not** rely on simple heuristics (e.g. “any reply”). Instead:

1. **Inputs** — For each candidate thread, pass the **original review text** (and severity if known, e.g. `[High]` / `[Medium]`) plus the **reply body to classify**. **Who may reply:** any **human** collaborator on the PR is in scope (a full team may have worked on the branch; do not restrict to PR author only). **Which reply:** use the **latest non-bot** note on the thread. In the common case, a **comment webhook fires once per new reply**, so the payload identifies the new comment — treat that as the reply under classification (it is the latest human addition). For batch or scheduled runs without a specific event, fetch the thread and take the **latest non-bot** comment after the bot’s finding.
2. **LLM task** — Classify whether the user is **disputing or dismissing** the finding in a way that should **stop counting** it toward the merge gate. Examples of intents that support **agreed** (non-exhaustive): *feature not a bug*, *won’t fix*, *acceptable risk / ignorable for now*, *false positive / you got this wrong*, *out of scope for this PR*.

3. **Structured output (contract)** — The model returns JSON only, with this shape:

   ```json
   {
     "verdict": "agreed" | "disagreed",
     "reply_text": "<string, required when verdict is disagreed>"
   }
   ```

   - **`agreed`** — We accept the user’s explanation; the finding **does not** count toward open high/medium for that thread (**reduce outstanding by one** for gate purposes, same as excluding the thread from aggregation).
   - **`disagreed`** — We do **not** accept the dismissal; the thread **still** counts. The response **must** include **`reply_text`**: the **text of the reply to the user’s comment** (i.e. the message we pass back for the bot to post on the thread so the user knows the gate still applies and why). When `verdict` is `agreed`, `reply_text` is omitted (or empty).

4. **Effect in the runner** — On `agreed`, apply the count reduction / exclusion. On `disagreed`, use `reply_text` as the body for a follow-up SCM comment (exact posting behavior TBD: always post vs dry-run log only).

5. **Bot noise** — Do not treat comments authored by the **automation / bot** identity as a dismissal attempt; skip classification (or no-op) for those events.

6. **Idempotency / caching** — Treated by the rest of the design rather than a separate store: comment webhooks identify a **specific comment**; after classification we **recompute the gate from SCM state** (threads, counts) and may **re-submit** the same review decision if nothing changed — which is harmless. Redelivery may repeat an LLM call; avoiding that is an **optional optimization**, not required for correctness.

7. **Safety / prompt stance** — Keep prompts **pragmatic**, not aggressive. Teams may have **legitimate reasons** to clear the gate quickly (deadlines, risk acceptance, follow-up tickets). Let the model judge `agreed` vs `disagreed` on the merits of the reply; do not bake in heavy anti-gaming or “must be substantive” barriers unless product explicitly tightens later.

### 5.2 When to skip work

Exact rule for “only if blocking”: query bot review/participant state vs always recompute when `UNKNOWN` (see Phase C).

---

## 6. Technical plan (phased)

### Phase A — Review-decision-only mode (runner + CLI)

- Add a mode (CLI flag and/or env, e.g. `--review-decision-only` / `CODE_REVIEW_REVIEW_DECISION_ONLY`) that:
  - Skips the **main code-review agent** (no full diff review / findings JSON).
  - May still invoke the **configured LLM** if Phase D reply classification is enabled for that run (see below).
  - Reuses `_quality_gate_high_medium_counts` (after any reply-based exclusions) and `_maybe_submit_review_decision` with `to_post=[]` unless we also surface net-new findings in some hybrid flow.
- Refactor if needed so decision submission is not duplicated between the full path and this path.
- `head_sha` handling: keep consistent with existing `submit_review_decision` per provider (GitHub/Gitea/GitLab use it in places).

**Tests:** `MockProvider`: counts → `APPROVE` / `REQUEST_CHANGES`; patch LLM for reply classification when testing Phase D integration.

### Phase B — SCM triggers for non-push activity

- Document and optionally wire **webhooks** / CI filters so comment and discussion events can invoke Phase A:
  - **GitHub:** e.g. `pull_request_review_comment`, `issue_comment`, optionally `pull_request_review`.
  - **GitLab:** note / MR discussion events as appropriate.
  - **Bitbucket:** comment / task events per API.
- **One event per new comment** is typical: each delivery can drive **one** classification for the **comment id in the payload**, without scanning the whole thread for “what changed” on that run. Full runs without a comment event still use **latest non-bot** on the thread (see §5.1).
- Update Jenkins / GitHub Actions docs with example filters; keep existing synchronize behavior for full reviews.

### Phase C — “Only if bot is blocking” (optional provider API)

- Add something like `get_bot_blocking_review_state(...) -> BLOCKING | NOT_BLOCKING | UNKNOWN` (names TBD) on `ProviderInterface` with defaults:
  - **GitHub / Gitea:** infer from latest review by the bot user for this PR.
  - **GitLab:** approval + requested-changes / review state (version-dependent).
  - **Bitbucket Cloud:** request-changes vs approved for the bot.
  - **Bitbucket Server:** participant `NEEDS_WORK` vs `APPROVED` for `SCM_BITBUCKET_SERVER_USER_SLUG`.
- In review-decision-only mode: if **NOT_BLOCKING** and there are no new high/medium findings to post, **skip** unresolved aggregation and submission (per product confirmation). If **UNKNOWN**, prefer safe default (recompute) unless documented otherwise.

### Phase D — Reply threads + LLM dismissal (per provider + model)

1. **Data** — Extend thread/discussion fetching so we can pair **bot/review comment body** with **subsequent human replies** and author metadata:
   - **GitHub:** GraphQL: comment list per thread with `author`, `createdAt`, body; identify bot vs human via login/app id (config: bot user id or pattern).
   - **GitLab:** MR discussion `notes` ordered; filter by author and bot note detection.
   - **Bitbucket Server / Cloud:** activity / note payloads where threading exists; Cloud may stay tasks-first until inline threads are rich enough.
   - **Gitea:** confirm API; document gaps.

2. **Gate integration** — On a **comment webhook**, map the payload to a thread + **that** new reply and classify once. On a **batch path**, consider each unresolved high/medium thread whose **latest non-bot** reply is **not yet classified** (same LLM contract). Return **structured JSON** per §5.1 (`verdict`, `reply_text` when `disagreed`).
   - If **`agreed`**, **omit that thread** from the high/medium dedupe set (one thread → at most one gate bump today).
   - If **`disagreed`**, keep the thread in counts and surface **`reply_text`** to the layer that posts SCM comments (or logs in dry-run).

3. **Config** — Env flags e.g. enable reply classification, max threads per run, model override optional; tie to `LLM_*` credentials already used by the runner.

4. **Operational** — Log `verdict` for audit; cap LLM calls per webhook; dry-run: log `reply_text` instead of posting where applicable; then recompute gate / `submit_review_decision` as today.

### Phase E — Docs and observability

- Extend [SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md](SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md) for the new flow.
- Log clearly when a run is review-decision-only vs full; when submission is skipped due to “not blocking.”

---

## 7. Related files (index)

| File | Role |
|------|------|
| `src/code_review/runner.py` | `_quality_gate_*`, `_maybe_submit_review_decision`, orchestration (future: reply-dismissal hook) |
| `src/code_review/models.py` | `get_configured_model()` — reuse for reply-classification calls |
| `src/code_review/config.py` | `review_decision_*`, `bitbucket_server_user_slug` |
| `src/code_review/providers/base.py` | `ReviewDecision`, `submit_review_decision`, `get_unresolved_review_items_for_quality_gate` |
| `src/code_review/providers/review_decision_common.py` | Shared helpers for GitHub-style JSON, GitLab note, effective body |
| `src/code_review/providers/github.py` | `submit_review_decision`, GraphQL quality gate |
| `src/code_review/providers/gitea.py` | `submit_review_decision`, REST quality gate |
| `src/code_review/providers/gitlab.py` | Quality gate + `submit_review_decision` |
| `src/code_review/providers/bitbucket.py` | Task-heavy gate + `submit_review_decision` |
| `src/code_review/providers/bitbucket_server.py` | Activities/tasks gate + participant `submit_review_decision` |
| `docker/jenkins/Jenkinsfile` | PR action filters for full runs |
| `tests/test_runner.py` | Decision orchestration |
| `tests/providers/test_*.py` | Per-provider submission and gate tests |

---

## 8. Optional follow-ups (unchanged from earlier notes)

- Extend `ReviewDecision` or capabilities if an SCM needs a third native state beyond approve / request-changes.
- **Bitbucket Cloud quality gate:** enrich from inline comments if the API exposes resolution reliably.
- Heavy live SCM integration tests (high maintenance).
