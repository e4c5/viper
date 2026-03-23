# Jenkins: review-decision-only on comment activity

Use this guide when you already have the normal Jenkins PR review pipeline working and want comment or thread activity to recompute the quality gate without running the full review agent.

This guide assumes the base Jenkins setup already exists:

- [Jenkins (existing)](JENKINS-EXISTING.md) for Gitea, GitHub, GitLab, and Bitbucket Cloud
- [Bitbucket Data Center](BITBUCKET-DATACENTER.md) for Bitbucket Server / DC

---

## Overview

There are two supported Jenkins setups:

- **Recommended:** create a second pipeline job dedicated to comment and thread events
- **Alternative:** reuse the existing pipeline job and switch selected webhook actions to review-decision-only

Both approaches use the same bundled Jenkinsfile: `docker/jenkins/Jenkinsfile`.

---

## 1. Recommended: create a second pipeline job

### 1.1 Create the job

Keep both jobs in the same folder.

1. On the full-review job, make sure `SCM_REVIEW_DECISION_ENABLED=true` is already enabled.
2. Start from that working full-review pipeline job.
3. Use **New Item** → **Copy from** to create a second job, for example `code-review-comments`.
4. Keep both jobs in the same folder so they share the folder-level configuration.

### 1.2 Enable review decisions on the new job

On the copied job, set:

- `CODE_REVIEW_JENKINS_DECISION_ONLY_ACTIONS` to the comment or thread actions that should run `--review-decision-only`

Optional:

- `CODE_REVIEW_REPLY_DISMISSAL_ENABLED=true` if you want reply-dismissal on `reply_added`
- `CODE_REVIEW_REVIEW_DECISION_ONLY_SKIP_IF_BOT_NOT_BLOCKING=true` to skip reply-driven runs when the bot is not currently blocking
- `SCM_REVIEW_DECISION_HIGH_THRESHOLD` and `SCM_REVIEW_DECISION_MEDIUM_THRESHOLD` as needed
- `SCM_BITBUCKET_SERVER_USER_SLUG` for Bitbucket Server / DC

See the [Configuration reference](CONFIGURATION-REFERENCE.md) for definitions and provider-specific notes.

Examples for `CODE_REVIEW_JENKINS_DECISION_ONLY_ACTIONS`:

- If `PR_ACTION` is the GitHub event name: `pull_request_review_comment` for review-comment replies and deletes, or `issue_comment` for PR conversation comments
- If `PR_ACTION` is the GitHub action field instead: `created,deleted`
- Bitbucket Server / DC (`PR_ACTION=$.eventKey`): `pr:comment:added,pr:comment:edited,pr:comment:deleted`
- Bitbucket Server / DC (only new comments/replies): `pr:comment:added`
- Other SCMs: use the exact values your webhook mapping sends into `PR_ACTION`

For Bitbucket Server / DC, keep full-review PR actions in your webhook filter (for example `^pr:(opened|modified|from_ref_updated)$`) and route comment activity through `CODE_REVIEW_JENKINS_DECISION_ONLY_ACTIONS`.

### 1.3 Configure the copied job webhook

On the copied job:

1. Keep the same core PR mappings as the full-review job: `SCM_OWNER`, `SCM_REPO`, `SCM_PR_NUM`, and `SCM_HEAD_SHA` when available.
2. Map `PR_ACTION` from the event field used by your SCM.
3. Add the optional `CODE_REVIEW_EVENT_*` mappings below.
4. Filter the webhook so only comment or thread events trigger this job.

`SCM_HEAD_SHA` may be empty for review-decision-only runs. The runner can resolve the current PR head from the SCM API when needed.

### 1.3.1 Optional filter setup (Generic Webhook Trigger)

In **Generic Webhook Trigger** for the copied comment-events job, configure:

- **Optional filter** -> **Text**: `$PR_ACTION`
- **Optional filter** -> **Regex**: use a regex that matches only comment/thread actions for your SCM mapping

Concrete examples:

- Gitea/GitHub/GitLab when `PR_ACTION=$.action` and you only want comment creates/deletes:
  - Regex: `^(created|deleted)$`
- GitHub when `PR_ACTION` is event name (for example `issue_comment` or `pull_request_review_comment`):
  - Regex: `^(issue_comment|pull_request_review_comment)$`
- Bitbucket Server / DC when `PR_ACTION=$.eventKey`:
  - Regex: `^pr:comment:(added|edited|deleted)$`
  - Or only new comments/replies: `^pr:comment:added$`

For the full-review job, keep the PR lifecycle filter instead (for Bitbucket Server / DC, for example `^pr:(opened|modified|from_ref_updated)$`).

### 1.4 Map review-decision event context

Add these Generic Webhook Trigger parameters when you want structured logs, reply-dismissal, or event-specific behavior:

| Variable | What to map |
|----------|-------------|
| `CODE_REVIEW_EVENT_NAME` | Raw SCM event name |
| `CODE_REVIEW_EVENT_ACTION` | Event action such as `created`, `deleted`, or provider equivalent |
| `CODE_REVIEW_EVENT_KIND` | `reply_added`, `comment_deleted`, `thread_resolved`, `thread_outdated`, `scheduled`, or `other` |
| `CODE_REVIEW_EVENT_COMMENT_ID` | PR review comment id |
| `CODE_REVIEW_EVENT_THREAD_ID` | Thread or discussion id |
| `CODE_REVIEW_EVENT_ACTOR_LOGIN` | Username or login |
| `CODE_REVIEW_EVENT_ACTOR_ID` | Numeric or string actor id |
| `CODE_REVIEW_EVENT_HEAD_SHA` | Head SHA from the event payload when available |
| `CODE_REVIEW_EVENT_SOURCE` | Usually `webhook_comment` or `webhook_thread` |

Important:

- For `reply_added` with reply-dismissal enabled, `CODE_REVIEW_EVENT_COMMENT_ID` must be the SCM’s PR review comment id, not some other object id such as a Bitbucket task id.
- For delete, resolve, or outdated-thread events, set `CODE_REVIEW_EVENT_KIND` accordingly. Those paths recompute from current SCM state and do not run the reply-dismissal LLM.

See [Configuration reference](CONFIGURATION-REFERENCE.md#51-review-decision-webhook-context-code_review_event_) for the variable definitions.

### 1.5 Point comment webhooks to the second job

Keep the existing full-review webhook pointed at the original job. Point comment or thread events at the copied job.

Use these base guides for the SCM-specific webhook shape:

- [Jenkins (existing)](JENKINS-EXISTING.md) for Gitea, GitHub, GitLab, and Bitbucket Cloud
- [Bitbucket Data Center](BITBUCKET-DATACENTER.md) for Bitbucket Server / DC

### 1.6 Validate the setup

Trigger a comment or reply event on a test PR and confirm:

- the comment-events job runs
- the logs show `--review-decision-only`
- the job recomputes the quality gate without posting normal inline findings

If you need a non-posting check first, run `code-review --review-decision-only --dry-run` manually outside Jenkins with the same environment.

---

## 2. Alternative: reuse the existing Jenkins job

If you do not want a second job, the existing Jenkins job can handle both full reviews and comment-driven recalculation.

### 2.1 Update the existing job

1. Keep the current Jenkinsfile: `docker/jenkins/Jenkinsfile`.
2. Set `SCM_REVIEW_DECISION_ENABLED=true`.
3. Set `CODE_REVIEW_JENKINS_DECISION_ONLY_ACTIONS` to the comment or thread actions that should run review-decision-only.
4. Add the optional `CODE_REVIEW_EVENT_*` webhook mappings if you want structured logs or reply-dismissal.
5. Optionally enable `CODE_REVIEW_REPLY_DISMISSAL_ENABLED=true`.

### 2.2 Expand the webhook mapping

Update the existing Generic Webhook Trigger so it can accept both:

- normal PR actions that should run the full review
- comment or thread actions listed in `CODE_REVIEW_JENKINS_DECISION_ONLY_ACTIONS`

The Jenkinsfile decides which mode to run based on the `PR_ACTION` value.

### 2.3 Know the tradeoff

This setup is simpler, but full reviews and comment-only recalculation share the same queue, logs, and build counts.

---

## 3. Related reading

- [SCM review decisions and merge blocking](SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md)
- [Configuration reference](CONFIGURATION-REFERENCE.md)
- [Jenkins (existing)](JENKINS-EXISTING.md)
- [Bitbucket Data Center](BITBUCKET-DATACENTER.md)
