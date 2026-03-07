# Bitbucket Data Center – Code review pipeline

When your SCM is **Bitbucket Data Center** (or Server), use this guide. You create one pipeline job that uses the same `Jenkinsfile` as for Gitea/GitHub/GitLab, but with a Bitbucket-specific credential ID and webhook payload mapping.

---

## Overview

| Item | For Bitbucket Data Center |
|------|----------------------------|
| Script Path | `docker/jenkins/Jenkinsfile` |
| Credential ID | `SCM_TOKEN` (same as other SCMs; use folder-scoped credentials so this job has its own token) |
| Webhook payload | `pullRequest`, `eventKey` (Bitbucket format) |
| Env | `SCM_URL` = Bitbucket REST API base |

**Caveat:** The code-review agent's Bitbucket provider targets **Bitbucket Cloud** API v2. Bitbucket Data Center is not a full drop-in supported backend: full native support for Data Center's `/rest/api/1.0` (diffs, comments, etc.) may require a dedicated provider. Until then, you may see failure modes around diffs or posting comments. This guide covers **triggering** the pipeline from Bitbucket DC and passing PR metadata; the agent still runs the same CLI with `--owner`, `--repo`, `--pr`, `--head-sha`.

If you use Gitea, GitHub, or GitLab instead, follow [Jenkins (existing)](JENKINS-EXISTING.md). If you don't have an SCM or want to try this in a green field use the [Quick Start](QUICKSTART.md) 

---

## 1. Prerequisites

- **Bitbucket Data Center / Server** (e.g. 7.21.x). Note: the agent's Bitbucket provider targets Cloud API v2; DC is not a full drop-in—diffs/comments on DC may require a dedicated provider and you may see failure modes until then.
- Jenkins with **Generic Webhook Trigger** plugin.
- Agent image: build with `docker build -t code-review-agent -f docker/Dockerfile.agent .` or pull from Docker Hub.

---

## 2. Create the Bitbucket pipeline job

1. **New Item** → **Pipeline** (e.g. name: `code-review`).
2. **Pipeline script from SCM** → point to this repo, **Script Path**: `docker/jenkins/Jenkinsfile`.  
   The script detects Bitbucket from the webhook payload and uses `SCM_TOKEN` and your `SCM_URL`.

---

## 3. Credentials

Add **Secret text** credentials with the IDs below (same as for Gitea/GitHub/GitLab). Prefer **folder-scoped** credentials so only this pipeline can use them:

- **Folder-scoped (recommended):** Create a Folder (e.g. `code-review`), add these credentials in **Folder → Credentials**, and create the pipeline job inside that folder.
- **Global:** **Manage Jenkins → Credentials → System → Global credentials** → Add Credentials.

| ID | Secret |
|----|--------|
| `SCM_TOKEN` | Bitbucket API token (repo read + comment on PRs) |
| `GOOGLE_API_KEY` | LLM API key (or your provider’s key) |

---

## 4. Job environment variables

In the Bitbucket job, set (job **Configure** → **Build Environment** or **Global properties**):

- **`SCM_URL`** (or **`SCM_URL_BITBUCKET`**): Bitbucket REST API base, e.g.  
  `https://bitbucket.example.com/rest/api/1.0`  
  (no trailing slash).

Optional: `SCM_PROVIDER` is set to `bitbucket` by the pipeline; `LLM_PROVIDER`, `LLM_MODEL` as needed.

---

## 5. Generic Webhook Trigger (Bitbucket payload)

In the job → **Configure** → **Build Triggers** → **Generic Webhook Trigger**:

**Post content parameters** (Expression type: **JSONPath**):

| Variable | Expression |
|----------|------------|
| `SCM_OWNER` | `$.pullRequest.toRef.repository.project.key` |
| `SCM_REPO` | `$.pullRequest.toRef.repository.slug` |
| `SCM_PR_NUM` | `$.pullRequest.id` |
| `SCM_HEAD_SHA` | `$.pullRequest.fromRef.latestCommit` |
| `PR_ACTION` | `$.eventKey` |

**Optional filter** (so only PR events trigger a build):

- Text: `$PR_ACTION`
- Regex: `^pr:(opened|modified|from_ref_updated)$`

Save and copy the **Webhook URL** shown in this section.

---

## 6. Bitbucket webhook

In Bitbucket Data Center, for the repo:

1. **Repository settings** → **Webhooks** → **Add webhook**.
2. **URL**: the Jenkins Generic Webhook Trigger URL from step 5.
3. **Content type**: `application/json`.
4. **Triggers**: pull request events (e.g. opened, updated, from ref updated).
5. Save and check **Delivery history** for 2xx responses.

---

## 7. Summary

- One pipeline job: **Script Path** `docker/jenkins/Jenkinsfile`, credential **`SCM_TOKEN`**, env **`SCM_URL`** (Bitbucket REST API base), and the Bitbucket webhook JSONPaths and filter from this doc.
- The pipeline detects the Bitbucket payload from `PR_ACTION` and uses your token and URL.

---

## 8. Limitations

As noted at the top of this guide: the Bitbucket provider targets Cloud API v2; Data Center is not a full drop-in, and full native support for DC's `/rest/api/1.0` (diffs, comments, etc.) may require a dedicated provider. This guide covers triggering the pipeline and passing PR metadata; the agent still runs the same CLI with `--owner`, `--repo`, `--pr`, `--head-sha`.
