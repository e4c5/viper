# SCM review decisions and merge blocking

This document explains how **automatic PR/MR review outcomes** from the code review agent relate to **whether a change can actually be merged** on each supported SCM. It complements the env vars `SCM_REVIEW_DECISION_*` (see [Configuration reference](CONFIGURATION-REFERENCE.md)) and the README summary of the quality gate.

---

## 1. What the runner does

When `SCM_REVIEW_DECISION_ENABLED=true`, the runner may submit a **PR-level review** after posting inline comments:

| Submitted event | Meaning in this tool |
|-----------------|----------------------|
| `APPROVE` | “OK to merge” from the bot’s perspective (open high/medium counts are below your thresholds). |
| `REQUEST_CHANGES` | “Not OK” — equivalent to asking for fixes before merge, in GitHub/Gitea vocabulary. |

Counts are based on **aggregated open** high/medium signals (this run plus unresolved items already on the PR, as implemented per provider). Details: `README.md` (Auto review decision) and `_maybe_submit_review_decision` in `src/code_review/runner.py`.

**Important:** Submitting `APPROVE` or `REQUEST_CHANGES` via API **does not by itself change merge permissions**. The **merge button** (or equivalent) is only constrained when the **repository or branch** is configured to require reviews, approvals, or merge checks. Admins and users with bypass permissions may still merge unless the SCM is configured to forbid that.

### Re-runs after the PR is updated

When PR authors push new commits to address comments, the agent re-runs automatically (the idempotency key changes with the new `head_sha`). The runner computes fresh high/medium counts and may submit a **different** decision from the previous run. State-transition behaviour per provider:

| Provider | Transition handling |
|----------|---------------------|
| **GitHub / Gitea** | A new review object is posted; the previous one becomes outdated. GitHub skips outdated threads when counting unresolved signals. |
| **GitLab** | When submitting `REQUEST_CHANGES`, `DELETE .../approve` is called first (soft-fail 404) so the bot cannot simultaneously hold an approval *and* a request-changes note on the same MR. |
| **Bitbucket Cloud** | Before writing the new state the opposite endpoint is cleared (`DELETE /request-changes` before approving; `DELETE /approve` before requesting changes). A 404 on the DELETE is silently ignored. |
| **Bitbucket Server** | `PUT .../participants/{slug}` replaces the status in place; transitions are inherently idempotent. |

### Quality gate recalculation and replies (review-decision-only)

The **full** review run (default agent flow) recomputes the gate from **unresolved review items on the PR** plus any **new** findings it is about to post, then may submit `APPROVE` / `REQUEST_CHANGES`. That path does **not** use reply-dismissal.

**Review-decision-only** (`CODE_REVIEW_REVIEW_DECISION_ONLY` or `--review-decision-only`) skips the main review agent and inline posting. It still loads **open** high/medium signals from the SCM (same aggregation rules as the README quality gate), applies your thresholds, and submits a review decision when `SCM_REVIEW_DECISION_ENABLED=true`. Operators typically trigger this mode from **webhooks or scheduled jobs** when a PR’s discussion state changes, and optionally pass **`CODE_REVIEW_EVENT_*`** so logs (and some behaviours below) know what happened.

| Trigger (typical) | What gets counted | Reply-dismissal LLM |
|-------------------|-------------------|---------------------|
| `CODE_REVIEW_EVENT_KIND` unset / empty context | All unresolved gate items from the provider | No |
| `comment_deleted`, `thread_resolved`, `thread_outdated`, `scheduled`, … | All unresolved items reflecting **current** SCM state after that event | No |
| `reply_added` | Same, **unless** reply-dismissal applies (next rows) | Only if enabled and conditions match |

**Optional reply-dismissal** (`CODE_REVIEW_REPLY_DISMISSAL_ENABLED=true`) runs **only** in review-decision-only when **`CODE_REVIEW_EVENT_KIND=reply_added`** and **`CODE_REVIEW_EVENT_COMMENT_ID`** identifies the new reply. It is implemented only where the provider exposes review-thread context (**GitHub** and **GitLab** today). On other providers, reply-added jobs still **recompute** the gate from the SCM, but the dismissal step is skipped (see metrics / logs: `skipped_no_capability`).

What happens when reply-dismissal runs:

1. **Bot replies are ignored** — if the webhook actor matches the bot identity used for posting, the dismissal path is skipped so the job does not classify the bot’s own comments (`skipped_bot_author` in metrics / structured logs).
2. **Thread context** — the runner loads the review thread (bot comment plus human replies). If the payload does not yield a usable thread, exclusion is not applied (`skipped_insufficient_thread`).
3. **LLM verdict** — a small, tool-free model classifies whether the human reply **adequately addresses** the review comment (`agreed` vs `disagreed`). On **LLM or parse failure**, no thread is excluded; counts stay conservative (`llm_error` / `parse_failed`).
4. **`agreed`** — for **this invocation only**, the thread’s stable id is **omitted** when counting open high/medium. The runner then submits `APPROVE` / `REQUEST_CHANGES` using those reduced counts. This exclusion is **not** persisted as SCM state: a later decision-only run without a successful `agreed` path will count that thread again if it is still unresolved on the SCM. Authors should still **resolve or address** threads in the native UI when your process expects the SCM to be the source of truth.
5. **`disagreed`** — counts are **not** reduced; if the provider supports thread replies, the runner may post a short follow-up on that thread (unless `--dry-run`).

**Optional early exit:** `CODE_REVIEW_REVIEW_DECISION_ONLY_SKIP_IF_BOT_NOT_BLOCKING` applies to **`reply_added`** when event context is present: if the provider reports the token user is **not** in a blocking review state, the job can skip recomputation entirely (providers without blocking-state query never skip on this path). Use this to avoid noise when the bot is no longer “requesting changes” in the SCM’s model.

Configuration details and env names: [Configuration reference](CONFIGURATION-REFERENCE.md) §5 and §5.1. Prometheus label **`outcome`** on `code_review_reply_dismissal_total` reflects which path ran (`agreed`, `disagreed`, skips, errors).

---

## 2. What this codebase implements today

`ProviderInterface.submit_review_decision` and `ProviderCapabilities.supports_review_decisions` gate whether the runner calls the SCM.

| Provider (`SCM_PROVIDER`) | Submits `APPROVE` / `REQUEST_CHANGES`? |
|---------------------------|----------------------------------------|
| `github` | Yes (`POST .../pulls/{id}/reviews` with `event`). |
| `gitea` | Yes (same-style review API; unsupported or old servers may return 404/405 — handled in code). |
| `gitlab` | Yes — `POST .../merge_requests/:iid/approve` (optional `sha`); `REQUEST_CHANGES` first calls `DELETE .../approve` (soft-fail) then posts an MR note + `/submit_review requested_changes` (needs a pending review on some GitLab versions). |
| `bitbucket` (Cloud) | Yes — `POST .../pullrequests/{id}/approve` and `.../request-changes`; prior conflicting state is cleared before the new state is written (see §1 re-run table). |
| `bitbucket_server` (Data Center / Server) | Yes when **`SCM_BITBUCKET_SERVER_USER_SLUG`** is set — `PUT .../pull-requests/{id}/participants/{slug}?version=…` with `APPROVED` / `NEEDS_WORK`. If unset, capability is false and the runner skips submission. |

All providers still post **inline comments** and participate in the **quality gate counts** as documented in the README.

### Review-decision-only, reply-dismissal, and thread events

In **review-decision-only** mode (`CODE_REVIEW_REVIEW_DECISION_ONLY` or `--review-decision-only`), the runner recomputes open high/medium counts from the provider and may submit `APPROVE` / `REQUEST_CHANGES` without running the main review agent. **How reply-based recalculation and optional thread exclusion interact with the gate** is described in **§1 (Quality gate recalculation and replies)**. Map webhook fields into `CODE_REVIEW_EVENT_*` per [Configuration reference](CONFIGURATION-REFERENCE.md) §5 and §5.1.

For a **code-level inventory**, test coverage, and the implementation plan checklist, see **[SCM review decisions — implementation plan](SCM-REVIEW-DECISIONS-IMPLEMENTATION-PLAN.md)**.

---

## 3. Per-SCM: native model, merge blocking, and configuration

The table below maps **UI/API concepts** to **when merge can be blocked**, points to **official documentation**, and states **Viper’s** auto-decision support.

### 3.1 GitHub

| Topic | Detail |
|-------|--------|
| **Reviewer outcomes** | **Comment**, **Approve**, **Request changes** ([About pull request reviews](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/reviewing-changes-in-pull-requests/about-pull-request-reviews)). |
| **Blocking merge** | Enable **branch protection** → **Require pull request reviews before merging**. If someone (including a bot) submits **Request changes**, the PR typically **cannot merge** until that review is addressed or **dismissed** by someone allowed to do so ([About protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)). |
| **Stricter enforcement** | Optionally **Do not allow bypassing the above settings** so admins and roles with bypass cannot skip rules without policy change. |
| **Viper** | Can submit `APPROVE` / `REQUEST_CHANGES` when enabled. Ensure the token identity is treated as a valid reviewer under your rules (e.g. counts toward required approvals if you rely on the bot’s approval). |

### 3.2 Gitea

| Topic | Detail |
|-------|--------|
| **Review API** | Pull request reviews use events aligned with GitHub-style **`APPROVE`** / **`REQUEST_CHANGES`** (see Gitea API for your version). |
| **Blocking merge** | **Settings → Branches** → protected branch rules: enable **Block merge on rejected reviews** so a requested-changes state blocks merge; combine with **required approvals** and other options as needed ([Protected branches](https://docs.gitea.com/usage/access-control/protected-branches)). |
| **Admin bypass** | Optionally **Administrators must follow branch protection rules** so admins cannot use **Force merge** to skip protections. |
| **Viper** | Can submit `APPROVE` / `REQUEST_CHANGES` when the server supports the endpoint (older or minimal builds may reject the call). |

### 3.3 GitLab

| Topic | Detail |
|-------|--------|
| **Approvals** | **Approve** satisfies **approval rules** when configured. **Required** approval rules that block merge are a **Premium / Ultimate** (and correct project) concern; on **Free**, approvals exist but do not enforce the same merge barrier ([Merge request approvals](https://docs.gitlab.com/ee/user/project/merge_requests/approvals/)). |
| **Request changes** | Reviewers can **Request changes**; **blocking the MR** until that is cleared is documented under **Prevent merge when you request changes** on [Merge request reviews](https://docs.gitlab.com/ee/user/project/merge_requests/reviews/) (tier and settings apply). Maintainers may **bypass** that check when permitted. |
| **API** | Approvals use the [Merge request approvals API](https://docs.gitlab.com/ee/api/merge_request_approvals.html) (e.g. `POST .../approve`). **Request changes** is part of the MR review flow, not the same endpoint family as GitHub’s single `event` field. |
| **Viper** | Calls `POST .../approve` (optional `sha`) and posts an MR note with `/submit_review requested_changes` for `REQUEST_CHANGES` (quick action may require a pending review on some GitLab versions). |

### 3.4 Bitbucket Cloud

| Topic | Detail |
|-------|--------|
| **Merge checks** | Repository **branch restrictions** and **merge checks** can require minimum approvals, **no “Changes requested”** from reviewers, no unresolved tasks, successful builds, etc. ([Suggest or require checks before a merge](https://support.atlassian.com/bitbucket-cloud/docs/suggest-or-require-checks-before-a-merge/)). |
| **Enforcement vs warning** | **Prevent a merge with unresolved merge checks** is tied to **Bitbucket Cloud Premium** for full enforcement; lower tiers may **warn** but still allow merge in some setups — confirm your plan and settings. |
| **Viper** | Uses Cloud `approve` and `request-changes` REST endpoints when `SCM_REVIEW_DECISION_ENABLED` is on. |

### 3.5 Bitbucket Data Center / Server

| Topic | Detail |
|-------|--------|
| **“Needs work” and merge checks** | Server uses **merge checks** on the target branch (minimum approvals, **no changes requested** / needs-work style hooks, unresolved tasks, builds, etc.). Atlassian maps the Server hook **`needs-work-merge-check`** to the idea of **no changes requested** when comparing to Cloud concepts ([merge checks comparison KB](https://support.atlassian.com/bitbucket-cloud/kb/merge-checks-comparison-between-bitbucket-server-vs-bitbucket-cloud/)). |
| **Documentation** | See **Checks for merging pull requests** in [Bitbucket Data Center documentation](https://confluence.atlassian.com/spaces/BitbucketServer/pages/776640039/Checks+for+merging+pull-requests) for the checklist available in your version. |
| **Bypass** | Project and permission settings determine whether users with elevated access can override failed checks; configure explicitly if you need a hard gate. |
| **Viper** | Submits participant status via `PUT .../participants/{slug}` when **`SCM_BITBUCKET_SERVER_USER_SLUG`** matches the token user; maps `REQUEST_CHANGES` → `NEEDS_WORK`. |

---

## 4. Summary matrix

| SCM | Merge blocked by “not OK” review state? | Typical non-default setup | Viper auto `APPROVE` / `REQUEST_CHANGES` |
|-----|----------------------------------------|---------------------------|----------------------------------------|
| **GitHub** | Yes, with branch protection | Required PR reviews + optional no-bypass | Yes |
| **Gitea** | Yes, if enabled | Protected branch + **Block merge on rejected reviews** (+ optional admin must follow rules) | Yes (if API supported) |
| **GitLab** | Yes, with tier/settings | Premium+ rules + “prevent merge when request changes” where applicable | Yes (approve REST + request-changes note; see §3.3) |
| **Bitbucket Cloud** | Yes, if plan/settings require checks | Branch restrictions + required merge checks (Premium for strict prevent) | Yes |
| **Bitbucket Data Center** | Yes, if merge checks enabled | Branch merge checks including needs-work / no changes requested | Yes when `SCM_BITBUCKET_SERVER_USER_SLUG` is set |

---

## 5. Operational checklist

1. **Pick the SCM** row above and enable the **branch / merge-check** settings your policy needs.
2. **Token permissions:** The integration user’s token must be allowed to **submit** the review outcome you depend on — **`APPROVE` / `REQUEST_CHANGES`** (or each SCM’s native equivalent) — on every provider you use (e.g. **GitHub, Gitea, GitLab, Bitbucket Cloud, Bitbucket Server**). That includes REST scopes for review/approval/participant APIs; on GitLab, MR notes for request-changes; on Bitbucket Server, permission to update the token user’s participant state when **`SCM_BITBUCKET_SERVER_USER_SLUG`** is set. Too-narrow scopes can leave inline comments working while decisions fail.
3. **Bypass policy:** Decide whether privileged users may merge despite failed reviews or checks, and set that explicitly per SCM — e.g. GitHub **do not allow bypassing** protected-branch rules; Gitea **administrators must follow branch protection rules**; GitLab **prevent merge when you request changes** (tier/settings) and maintainer bypass where applicable; Bitbucket Cloud **prevent merge with unresolved merge checks** vs warn-only (plan-dependent); Bitbucket Server merge checks and who may override them (project/permission settings). Use §3 for your provider’s knobs.
4. **Tune thresholds:** `SCM_REVIEW_DECISION_HIGH_THRESHOLD` and `SCM_REVIEW_DECISION_MEDIUM_THRESHOLD` control how aggressive `REQUEST_CHANGES` is; start conservative in production.
5. **Dry run:** Use `--dry-run` to verify counts and logs without posting or submitting decisions.

---

## 6. Related reading

- [SCM review decisions — implementation plan](SCM-REVIEW-DECISIONS-IMPLEMENTATION-PLAN.md) — what is implemented in code and backlog per provider
- [Configuration reference](CONFIGURATION-REFERENCE.md) — `SCM_REVIEW_DECISION_*`
- [Developer guide](DEVELOPER_GUIDE.md) — `ProviderInterface`, extension points
- [Bitbucket Data Center (Jenkins)](BITBUCKET-DATACENTER.md) — webhook and env for `bitbucket_server`
- `README.md` — Auto review decision and per-SCM “open” semantics for the quality gate
