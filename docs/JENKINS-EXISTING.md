# Using the code-review agent with your existing Jenkins

This guide is for teams that **already run Jenkins** (on-prem or in CI) and want to add the code-review agent. You do **not** need the Docker Compose stack from the [Quick Start](QUICKSTART.md); you only add a pipeline job and credentials to your current Jenkins.

Supported SCMs: **Gitea**, **GitHub**, **GitLab**, **Bitbucket Cloud**, and **Bitbucket Data Center**. Most setups use one SCM. If yours is **Bitbucket Data Center** (or Server), follow [Bitbucket Data Center](BITBUCKET-DATACENTER.md) for credential ID and webhook setup; otherwise use this guide.

---

## Overview

| Step | What to do |
|------|------------|
| 1 | Create a **Pipeline** job and use the Jenkinsfile from this repo |
| 2 | Add **credentials** (SCM token, LLM API key) in Jenkins |
| 3 | Set **SCM and LLM** environment variables for the job (or globally) |
| 4 | (Optional) Configure **webhooks** so PRs trigger the job automatically |
| 5 | Ensure the job’s Jenkins **node** can run the review: either **Docker or Podman** plus the agent image, or the **CLI** installed on the node (no containers) |

---

## 1. Create the pipeline job

1. In Jenkins: **New Item** → **Pipeline** (e.g. name: `code-review`).
2. **Pipeline** section:
   - Choose **Pipeline script from SCM**.
   - Point **SCM** to this repository (Git URL and branch).
   - Set **Script Path** to `docker/jenkins/Jenkinsfile`.
3. Do **not** define `SCM_OWNER`, `SCM_REPO`, `SCM_PR_NUM`, `SCM_HEAD_SHA`, or `PR_ACTION` as parameters in the UI when using webhooks—the Jenkinsfile declares them and the Generic Webhook Trigger fills them.

If you prefer to paste the script: **Pipeline script** and copy the contents of `docker/jenkins/Jenkinsfile` from this repo.

---

## 2. Add credentials

In **Manage Jenkins → Credentials → System → Global credentials**:

| Credential ID | Kind | Purpose |
|---------------|------|--------|
| `SCM_TOKEN` | Secret text | SCM API token (Gitea, GitHub, GitLab, or Bitbucket Cloud) with repo read + comment on PRs |
| `GOOGLE_API_KEY` | Secret text | LLM API key (or use your provider’s key and set `LLM_PROVIDER` / `LLM_MODEL`) |

If your SCM is **Bitbucket Data Center**, use credential ID `SCM_TOKEN_BITBUCKET` and follow [Bitbucket Data Center](BITBUCKET-DATACENTER.md).

---

## 3. Set SCM and LLM environment variables

The job must have access to your SCM URL and (optionally) LLM settings. Set them on the **job** or in **Manage Jenkins → System → Global properties → Environment variables**.

| Variable | Example (Gitea) | Example (GitHub) |
|----------|-----------------|------------------|
| `SCM_PROVIDER` | `gitea` | `github` |
| `SCM_URL` | `https://gitea.example.com` or `http://gitea:3000` | `https://api.github.com` |

For **GitLab**: `SCM_PROVIDER=gitlab`, `SCM_URL=https://gitlab.com` (or your GitLab URL).  
For **Bitbucket Cloud**: `SCM_PROVIDER=bitbucket`, `SCM_URL=https://api.bitbucket.org`.

LLM (optional if you rely on job parameters): `LLM_PROVIDER=gemini`, `LLM_MODEL=gemini-2.5-flash`, and ensure the API key is in credentials (e.g. `GOOGLE_API_KEY`).

---

## 4. (Optional) Webhooks so PRs trigger the job

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
