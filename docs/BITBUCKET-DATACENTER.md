# Bitbucket Data Center – Code review pipeline

Use a **separate Jenkins job** for Bitbucket Data Center (or Server). Both Gitea/GitHub and Bitbucket jobs use the **same** `Jenkinsfile`; the script detects the webhook source from `PR_ACTION` and uses the correct credential and SCM URL.

---

## Overview

| Item | Gitea / GitHub job | Bitbucket job |
|------|--------------------|----------------|
| Script Path | `docker/jenkins/Jenkinsfile` | `docker/jenkins/Jenkinsfile` (same) |
| Credential ID | `SCM_TOKEN` | `SCM_TOKEN_BITBUCKET` |
| Webhook payload | `pull_request`, `action` | `pullRequest`, `eventKey` |
| Docs | [Quick Start](QUICKSTART.md) | This document |

Two jobs, one script: each job has its own webhook URL and credential; the pipeline chooses token and SCM settings from the payload.

---

## 1. Prerequisites

- Bitbucket Data Center / Server (e.g. 7.21.x).
- Jenkins with **Generic Webhook Trigger** plugin.
- Agent image: build with `docker build -t code-review-agent -f docker/Dockerfile.agent .` or pull from Docker Hub.

---

## 2. Create the Bitbucket pipeline job

1. **New Item** → **Pipeline** (e.g. name: `code-review-bitbucket`).
2. **Pipeline script from SCM** → point to this repo, **Script Path**: `docker/jenkins/Jenkinsfile`  
   (same as the Gitea/GitHub job; the script detects Bitbucket from the webhook payload).

---

## 3. Credentials

In **Manage Jenkins → Credentials**, add **Secret text**:

| ID | Secret |
|----|--------|
| `SCM_TOKEN_BITBUCKET` | Bitbucket API token (repo read + comment on PRs) |
| `GOOGLE_API_KEY` | LLM API key (or your provider’s key) |

This job uses only `SCM_TOKEN_BITBUCKET`; the Gitea/GitHub job uses `SCM_TOKEN`.

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

- **One Jenkinsfile** (`docker/jenkins/Jenkinsfile`) for both Gitea/GitHub and Bitbucket; no duplicate pipeline script.
- **Bitbucket job**: credential `SCM_TOKEN_BITBUCKET`, env `SCM_URL`, Bitbucket webhook URL and JSONPaths (this doc).
- **Gitea/GitHub job**: credential `SCM_TOKEN`, webhook and JSONPaths as in [Quick Start](QUICKSTART.md).

Each job has its own webhook URL and credential; the pipeline picks the right token and SCM URL from `PR_ACTION`.

---

## 8. Limitations

The code-review agent’s Bitbucket provider is built for **Bitbucket Cloud** API v2. Full native support for Data Center’s `/rest/api/1.0` (diffs, comments, etc.) may require a dedicated provider later. This guide covers **triggering** the pipeline from Bitbucket DC and passing PR metadata; the agent still runs the same CLI with `--owner`, `--repo`, `--pr`, `--head-sha`.
