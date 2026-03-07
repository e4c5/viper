# Using the code-review agent with your existing Jenkins

This guide is for teams that **already run Jenkins** (on-prem or in the cloud) and want to add the code-review agent. You do **not** need the Docker Compose stack from the [Quick Start](QUICKSTART.md); you only add a pipeline job and credentials to your current Jenkins.

Supported SCMs: **Gitea**, **GitHub**, **GitLab**, **Bitbucket Cloud**, and **Bitbucket Data Center**. Most setups use one SCM; this guide covers that case. If you use **multiple SCMs**, see [Jenkins with multiple SCMs](JENKINS-MULTIPLE-SCMS.md). If your SCM is **Bitbucket Data Center** (or Server), follow [Bitbucket Data Center](BITBUCKET-DATACENTER.md) for webhook setup; otherwise use this guide.

---

## Overview

| Step | What to do |
|------|------------|
| 1 | Create a **Pipeline** job and use the Jenkinsfile from this repo |
| 2 | Add **credentials** (SCM token, LLM API key) in Jenkins |
| 3 | Set **SCM and LLM** environment variables for the job (or globally) |
| 4 | Configure **webhooks** so PRs trigger the job automatically |
| 5 | Ensure the job’s Jenkins **node** can run the review: either **Docker or Podman** plus the agent image, or the **CLI** installed on the node (no containers) |

---

## 1. Create the pipeline job

Create a **Pipeline** job for code review (e.g. name: `code-review`). The simplest path is:

1. **New Item** → **Pipeline** (from the dashboard, or inside a folder if you already use folders).
2. Configure the job as below.

You can later move the job into a folder (open the job → **Move** → pick the folder) if you decide to use folder-scoped credentials.

Then configure the pipeline:

1. **Pipeline** section:
   - Choose **Pipeline script from SCM**.
   - Point **SCM** to this repository (Git URL and branch).
   - Set **Script Path** to `docker/jenkins/Jenkinsfile`.
2. Do **not** define `SCM_OWNER`, `SCM_REPO`, `SCM_PR_NUM`, `SCM_HEAD_SHA`, or `PR_ACTION` as parameters in the UI when using webhooks—the Jenkinsfile declares them and the Generic Webhook Trigger fills them.

Use **Pipeline script from SCM** (not inline **Pipeline script**): the Jenkinsfile loads `docker/jenkins/mainPipeline.groovy` from the repo, so the pipeline depends on SCM checkout. Pasting only the Jenkinsfile into inline script would fail because the loaded file would not be available.

---

## 2. Add credentials

You can store credentials **globally** (visible to all jobs) or **per folder** (visible only to jobs in that folder). Prefer folder-scoped credentials so the SCM and LLM tokens are only available to the code-review pipeline(s).

### Option A – Pipeline-specific (recommended)

1. Create a **Folder** (if you don’t have one yet): **New Item** → **Folder** (e.g. name: `code-review`).
2. Ensure the **pipeline job lives inside this folder**. If you created it on the main dashboard, **move it**: open the job → **Move** (left sidebar) → select the folder → **Move**.
3. Open the folder → **Credentials** (in the folder’s left menu). If you don’t see it, install the **Folders** plugin (**Manage Jenkins → Plugins**).
4. On the **Folder** credentials page, click the **Global** domain row, then use **Add credentials** (typically in the left sidebar or page actions). Choose **Kind: Secret text** and create the credentials in the table below in this folder. Jobs in this folder will use them.

### Option B – Global

In **Manage Jenkins → Credentials → System → Global credentials (unrestricted)** → **Add Credentials**. Any job on the instance can use these.

| Credential ID | Kind | Purpose |
|---------------|------|--------|
| `SCM_TOKEN` | Secret text | SCM API token (Gitea, GitHub, GitLab, or Bitbucket Cloud) with repo read + comment on PRs |
| `GOOGLE_API_KEY` | Secret text | LLM API key (or use your provider’s key and set `LLM_PROVIDER` / `LLM_MODEL`) |

If your SCM is **Bitbucket Data Center**, use the same credential ID `SCM_TOKEN` (with your Bitbucket token) and follow [Bitbucket Data Center](BITBUCKET-DATACENTER.md) for webhook setup.

---

## 3. Configure SCM and LLM environment variables

The Jenkinsfile reads SCM and LLM settings from environment variables. At minimum you should set **`SCM_PROVIDER`** and **`SCM_URL`**; you can optionally override **`LLM_PROVIDER`** and **`LLM_MODEL`**.

The simplest path is to set them **globally**:

- **Manage Jenkins → System → Global properties → Environment variables** → add the variables below.

Pipeline jobs do not have a “Build Environment” section in the job configuration UI (unlike Freestyle jobs). For the single-SCM setup, use the **global** variables above. The main pipeline (`Script Path`: `docker/jenkins/Jenkinsfile`) is designed for one set of global SCM/LLM env vars. For **multiple SCMs**, use the wrapper approach in [Jenkins with multiple SCMs](JENKINS-MULTIPLE-SCMS.md) instead.

| Variable | Example (Gitea) | Example (GitHub) |
|----------|-----------------|------------------|
| `SCM_PROVIDER` | `gitea` | `github` |
| `SCM_URL` | `https://gitea.example.com` or `http://gitea:3000` | `https://api.github.com` |

For **GitLab**: `SCM_PROVIDER=gitlab`, `SCM_URL=https://gitlab.com` (or your GitLab URL).  
For **Bitbucket Cloud**: `SCM_PROVIDER=bitbucket`, `SCM_URL=https://api.bitbucket.org`.  
For **Bitbucket Data Center**: see [Bitbucket Data Center](BITBUCKET-DATACENTER.md) for the `SCM_URL` format.

LLM (optional): set `LLM_PROVIDER` and `LLM_MODEL` if you want to override the defaults (for example `LLM_PROVIDER=gemini`, `LLM_MODEL=gemini-2.5-flash`) and ensure the API key is in credentials (e.g. `GOOGLE_API_KEY`).

---

## 4. Webhooks so PRs trigger the job

To run the review when a PR is opened or updated, use the **Generic Webhook Trigger** plugin.

1. Install **Generic Webhook Trigger** if not already installed: **Manage Jenkins → Plugins**.
2. In your pipeline job: **Configure → Build Triggers** → enable **Generic Webhook Trigger**.
3. **Post content parameters**: add the variables and JSONPath expressions for your SCM (see below).
4. **Optional filter**: Variable `PR_ACTION`, Regexp `^(opened|synchronize)$` (Gitea/GitHub/GitLab) so only open/sync triggers a build.
5. Copy the **Webhook URL** from the trigger section and configure it in your SCM (repo **Settings → Webhooks**).

### Gitea / GitHub / GitLab (similar payloads)

| Variable | Expression (JSONPath) |
|----------|------------------------|
| `SCM_OWNER` | `$.pull_request.base.repo.owner.login` (GitHub/GitLab) or Gitea equivalent |
| `SCM_REPO` | `$.pull_request.base.repo.name` |
| `SCM_PR_NUM` | `$.pull_request.number` |
| `SCM_HEAD_SHA` | `$.pull_request.head.sha` |
| `PR_ACTION` | `$.action` |

If your SCM is **Bitbucket Data Center**, see [Bitbucket Data Center](BITBUCKET-DATACENTER.md) for the JSONPath expressions and filter (different webhook payload).

---

## 5. Running the agent: Docker/Podman vs CLI on the node

The Jenkinsfile can run the agent in two ways:

| Mode | When to use | What you need |
|------|--------------|---------------|
| **Container** | Jenkins agents have Docker or Podman | Agent image on the node: `docker pull e4c5/code-review-agent` and tag as `code-review-agent`, or build from repo: `docker build -t code-review-agent -f docker/Dockerfile.agent .` |
| **Inline (no container)** | No Docker/Podman on agents, or you prefer not to use it | Install the `code-review` CLI on each agent that runs the job and set **`USE_INLINE_AGENT=true`** (job or global env). See [Jenkins without Docker](JENKINS-NO-DOCKER.md). |

If you don’t set `USE_INLINE_AGENT=true` and the node has no Docker/Podman, the build will fail; the Jenkinsfile suggests setting `USE_INLINE_AGENT=true` and points to the docs.

---

## Summary

- **Existing Jenkins**: Add one Pipeline job (Script Path: `docker/jenkins/Jenkinsfile`), credentials `SCM_TOKEN` and `GOOGLE_API_KEY`, and SCM/LLM env vars.
- **Webhooks**: Use Generic Webhook Trigger and your SCM’s webhook UI; if your SCM is Bitbucket Data Center, see [Bitbucket Data Center](BITBUCKET-DATACENTER.md).
- **Execution**: Use the prebuilt image (or build it) on agents with Docker/Podman, or install the CLI and set `USE_INLINE_AGENT=true` as in [Jenkins without Docker](JENKINS-NO-DOCKER.md).

For a full local stack (Gitea + Jenkins via Docker Compose), see [Quick Start](QUICKSTART.md).
