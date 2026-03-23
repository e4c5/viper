# SCM review decisions and merge blocking

Viper can submit a **PR-level review outcome** from the bot—**`APPROVE`** or **`REQUEST_CHANGES`**—based on how many **open** high- and medium-severity review signals still apply to the PR. For per-host definitions of “open”, see **Auto review decision** in [README.md](../README.md).

Each run **recomputes** those counts and may change the outcome. A **full** review after new commits does that automatically; for **discussion-only** changes (replies, deleted comments, resolved or outdated threads), the same gate logic runs in a lighter **review-decision-only** mode, typically triggered from CI or webhooks once you have configured it (**§2**).

**Merge blocking** is decided by your SCM’s branch protection and merge checks, not by the API call alone.

---

## 1. Concepts and recalculation

This section describes **runtime behaviour** only: what the runner does and when counts refresh. **§2** is where Viper settings, SCM host settings, env vars, and CLI flags are documented; the full variable list is in the [Configuration reference](CONFIGURATION-REFERENCE.md).

### 1.1 What a “review decision” is in Viper

When automatic review decisions are **enabled** (a Viper setting—**§2**), the runner can submit a **PR-level** outcome after it knows how many **open** high- and medium-severity signals apply to the PR (see [README.md](../README.md) for how “open” is defined per SCM).

| Submitted outcome | Meaning here |
|-------------------|--------------|
| **`APPROVE`** | From the bot’s perspective, open high/medium counts are **below** the configured cutoffs — OK to merge in terms of this gate. |
| **`REQUEST_CHANGES`** | Counts meet or exceed those cutoffs — not OK until those signals are addressed, in the same sense as GitHub’s “request changes”. |

How aggressive those cutoffs are is **configurable** in **§2**; this section does not define default or recommended values.

### 1.2 Merge blocking comes from the SCM, not from the API alone

Submitting **`APPROVE`** or **`REQUEST_CHANGES`** over the API **does not by itself** turn merge on or off. Whether the merge button (or equivalent) is blocked depends on **your repository / branch settings**: protected branches, required reviews, approval rules, merge checks, and your **plan/tier** on hosts like GitLab or Bitbucket Cloud. People with **bypass** rights may still merge unless you disable that in policy.

Treat Viper’s decision as **input** into that native model: the bot’s identity and its approve / needs-work state must be recognized under the host’s rules if you want merges to follow this gate. Aligning SCM branch rules with that intent is part of **§2**.

### 1.3 Recalculation after code updates (new commits)

On a **normal** review run, the runner recomputes counts from **unresolved review items** the provider returns for the PR **plus** any **new** findings it is about to post (deduped). Then it may submit **`APPROVE`** / **`REQUEST_CHANGES`**.

When authors **push new commits**, CI typically reruns the full job; the idempotency key changes with the new **`head_sha`**, counts are fresh, and the submitted decision may change. How the SCM **replaces** an older bot review varies:

| Provider | Notes |
|----------|--------|
| **GitHub / Gitea** | A new review is posted; older ones can become outdated. GitHub’s gate counts skip outdated threads. |
| **GitLab** | Before **`REQUEST_CHANGES`**, an existing bot **approve** is cleared (`DELETE .../approve`, 404 ignored) so the bot is not both approved and requesting changes. |
| **Bitbucket Cloud** | Opposite state is cleared first (`DELETE` approve or request-changes); 404 ignored. |
| **Bitbucket Server / DC** | Participant status is updated in place (`PUT .../participants/{slug}`). |

The push path does **not** run **reply-dismissal**; that optional behaviour exists only on **review-decision-only** runs when someone replies (**§1.4**).

### 1.4 Recalculation when someone replies on a thread

After comment activity, you can run the tool in **review-decision-only** mode so it skips the main review agent: less work and fewer tokens than a full review. **§2** describes how to hook that up from webhooks or CI.

When the triggering event is a **reply on a thread**, decision-only still reloads **open** high/medium signals from the SCM and may submit **`APPROVE`** or **`REQUEST_CHANGES`**. If the outcome does not change after a human reply, the host usually still reports the same unresolved items—for example **Bitbucket Cloud** still counts **open inline comments and open tasks** until they are resolved in the UI; **Bitbucket Server / DC** is the same for comments plus tasks. Replies are not ignored; the SCM state simply has not changed yet.

**Optional reply-dismissal.** If turned on in configuration (**§2**), an extra step can load the **PR review-comment** thread, run a small LLM, and—if it judges the reply sufficient—**omit that thread from counts for this run only** (nothing is written back to the SCM as “resolved”). If it judges the reply insufficient, the runner may post a brief follow-up **on that comment thread** where the API allows.

Implemented for **GitHub**, **GitLab**, **Bitbucket Cloud**, and **Bitbucket Server / DC**. For **Bitbucket**, threading and exclusion apply to **inline pull request comments** (parent / reply links in the REST model). The quality gate uses **`comment:{root_comment_id}`** for those items so the excluded id matches what the gate counts. **Gitea** does not implement this path yet (`skipped_no_capability`); recalculation still uses full SCM-derived counts.

**Requirement:** the webhook / event must supply the **comment** id of the new reply (or any comment in the thread, depending on your mapping)—not e.g. a **Bitbucket PR task** id. If the id does not resolve to a comment thread with at least two messages, the dismissal step is skipped (`skipped_insufficient_thread`) while decision-only recalculation still runs.

### 1.5 Recalculation when comments are deleted or threads change

For **deleted comments**, **resolved** or **outdated** threads, **scheduled** jobs, or **no specific event**, a decision-only run recomputes from the **provider’s view of unresolved items** only. No reply-dismissal LLM runs. After a delete or resolve, the SCM usually drops that signal from “open” counts; the next run may then submit **`APPROVE`** instead of **`REQUEST_CHANGES`**. How webhook payloads map to these runs is **§2**.

---

## 2. Setup

This section covers the two supported ways to run comment-driven recalculation without duplicating configuration or CI setup details from other guides.

For variables and flags, see the [Configuration reference](CONFIGURATION-REFERENCE.md). For job creation and webhook wiring, see [GitHub Actions](GITHUB-ACTIONS.md), [Jenkins (existing)](JENKINS-EXISTING.md), and [Bitbucket Data Center](BITBUCKET-DATACENTER.md). Merge blocking still depends on your SCM’s native branch protection or merge-check configuration.

### 2.1 Recommended: a separate comment-events job

Create a second workflow or pipeline job for **discussion-only** events such as replies, deletions, resolves, outdated threads, or scheduled recalculation. Keep the original full-review job for new commits unchanged.

For Jenkins, prefer putting the full-review job and the comment-events job in the same folder. That keeps the two pipelines easy to track while still sharing the folder-level configuration.

The comment-events job should run **review-decision-only** mode, pass webhook context through **`CODE_REVIEW_EVENT_*`**, and enable **`SCM_REVIEW_DECISION_ENABLED`** so the quality gate can be recomputed without running the full review agent. If reply-dismissal is required for human replies, also enable **`CODE_REVIEW_REPLY_DISMISSAL_ENABLED`** on this job.

This approach keeps full reviews and comment-only recalculation in separate Jenkins jobs, which makes it easier to track volume and behavior for each path independently.

Use the existing setup guides for the concrete wiring:

- [Configuration reference](CONFIGURATION-REFERENCE.md), especially review-decision-only and webhook context
- [Jenkins: review-decision-only on comment activity](JENKINS-REVIEW-DECISION-ONLY.md) for the dedicated second-pipeline setup
- [GitHub Actions](GITHUB-ACTIONS.md) for a dedicated comment-triggered workflow example
- [Jenkins (existing)](JENKINS-EXISTING.md) for pipeline setup
- [Bitbucket Data Center](BITBUCKET-DATACENTER.md) for Bitbucket Server / DC webhook mapping

### 2.2 Alternative: reuse the existing pipeline

If a separate job is not desirable, route comment events through the existing pipeline and switch those runs to **review-decision-only**. The full review path continues to handle code updates, while comment-driven runs skip the main review agent and only recompute the gate.

This reduces CI surface area, but full reviews and comment-only recalculations will then share the same queue, logs, and job counts.

Use the same references for the concrete mechanics:

- [Configuration reference](CONFIGURATION-REFERENCE.md) for the event variables, reply-dismissal, and `CODE_REVIEW_JENKINS_DECISION_ONLY_ACTIONS`
- [Jenkins: review-decision-only on comment activity](JENKINS-REVIEW-DECISION-ONLY.md) for Jenkins wiring
- [GitHub Actions](GITHUB-ACTIONS.md) for event-based workflow triggering
- [Jenkins (existing)](JENKINS-EXISTING.md) and [Bitbucket Data Center](BITBUCKET-DATACENTER.md) for Jenkins webhook setup

Use **`--dry-run`** when validating either approach.

---

## Related reading

- [Configuration reference](CONFIGURATION-REFERENCE.md) — all `SCM_REVIEW_DECISION_*` and `CODE_REVIEW_*` variables
- [Developer guide](DEVELOPER_GUIDE.md) — `ProviderInterface`, `ReviewOrchestrator`, extension points
- `README.md` — quality gate semantics and per-SCM “open” behaviour
