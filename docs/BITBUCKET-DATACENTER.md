## Bitbucket Data Center Integration (Jenkins Webhook)

This document describes how to trigger the **code-review agent** from **Bitbucket Data Center / Server** pull requests using **Jenkins + Generic Webhook Trigger**.

> **Status**
>
> - Bitbucket **Cloud** is supported via the built‑in `BitbucketProvider` (`SCM_PROVIDER=bitbucket`, `SCM_URL=https://api.bitbucket.org/2.0`).
> - Bitbucket **Data Center / Server** can currently:
>   - Trigger the Jenkins pipeline on PR events.
>   - Pass PR metadata (`owner`, `repo`, `PR number`, `head SHA`) into the pipeline.
> - Full, native Data Center API support (diffs, files, comments via `/rest/api/1.0/...`) is a **future enhancement**; today the provider is Cloud‑oriented.

---

## 1. Prerequisites

- A running Bitbucket Data Center / Server instance (e.g. 7.21.x).
- Jenkins with:
  - **Generic Webhook Trigger** plugin installed.
  - The pipeline from this repo (`docker/jenkins/Jenkinsfile`) configured as a **Pipeline** job.
- Jenkins credentials:
  - `SCM_TOKEN` (Secret text) – Bitbucket API token or user PAT with repo read + comment permissions.
  - LLM API key (e.g. `GOOGLE_API_KEY`) as Secret text.
- The Jenkins job either:
  - Uses the **Docker / Podman** path (default), or
  - Uses **inline mode** (`USE_INLINE_AGENT=true`) as described in `docs/JENKINS-NO-DOCKER.md`.

---

## 2. Configure the Jenkins pipeline job

Create (or reuse) a **Pipeline** job that uses `docker/jenkins/Jenkinsfile`:

- **Pipeline script from SCM** → point to this repo; `Script Path = docker/jenkins/Jenkinsfile`,  
  **or**
- **Pipeline script** → paste the contents of `docker/jenkins/Jenkinsfile`.

The pipeline expects these parameters/env vars to be populated from the webhook:

- `SCM_OWNER` – Bitbucket project key (e.g. `AN`).
- `SCM_REPO` – repository slug (e.g. `antitkythera-examples`).
- `SCM_PR_NUM` – pull request id (e.g. `3`).
- `SCM_HEAD_SHA` – head commit SHA of the PR source branch.
- `PR_ACTION` – webhook action / event key.

### 2.1. Generic Webhook Trigger – JSONPath mappings (Bitbucket DC)

In the **Pipeline job → Configure → Build Triggers → Generic Webhook Trigger** section:

**Post content parameters** (all `Expression type = JSONPath`):

- **`SCM_OWNER`**
  - Variable: `SCM_OWNER`
  - Expression: `$.pullRequest.toRef.repository.project.key`
- **`SCM_REPO`**
  - Variable: `SCM_REPO`
  - Expression: `$.pullRequest.toRef.repository.slug`
- **`SCM_PR_NUM`**
  - Variable: `SCM_PR_NUM`
  - Expression: `$.pullRequest.id`
- **`SCM_HEAD_SHA`**
  - Variable: `SCM_HEAD_SHA`
  - Expression: `$.pullRequest.fromRef.latestCommit`
- **`PR_ACTION`**
  - Variable: `PR_ACTION`
  - Expression: `$.eventKey`

These JSONPaths match a Bitbucket DC pull request payload similar to:

```json
{
  "eventKey": "pr:opened",
  "pullRequest": {
    "id": 3,
    "fromRef": {
      "latestCommit": "0a9c380b7b37aea57cf61fd644843808c9dfbb67",
      "repository": { "...": "..." }
    },
    "toRef": {
      "repository": {
        "project": { "key": "AN" },
        "slug": "antitkythera-examples"
      }
    }
  }
}
```

### 2.2. Generic Webhook Trigger – event filter for Bitbucket

Still in the **Generic Webhook Trigger** section, configure the **Optional filter** to allow the desired Bitbucket PR events:

- **Optional filter text**: `$PR_ACTION`
- **Optional filter regexp**: `^pr:(opened|modified|from_ref_updated)$`

This keeps the job from firing on unrelated webhook events.

### 2.3. Allowed actions in the Jenkinsfile

The Jenkinsfile accepts both GitHub/Gitea‑style actions and Bitbucket Data Center `eventKey` values:

- GitHub / Gitea: `opened`, `synchronize`
- Bitbucket DC: `pr:opened`, `pr:modified`, `pr:from_ref_updated`

If you customize Bitbucket’s event handling further, update the `allowedActions` list in `docker/jenkins/Jenkinsfile` accordingly.

---

## 3. Configure the Bitbucket Data Center webhook

For the target repository in Bitbucket Data Center:

1. Open **Repository settings → Webhooks** and create a new webhook.
2. **URL**: paste the Jenkins **Generic Webhook Trigger** URL from the job configuration.
3. **Content type**: `application/json`.
4. **Events / Triggers**:
   - Enable pull request events such as:
     - PR opened (`pr:opened`)
     - PR updated / modified (`pr:modified`)
     - PR from ref updated (`pr:from_ref_updated`, e.g. new commits on source branch)
5. Save the webhook.
6. Open the webhook’s **delivery history** and confirm that test deliveries or new PRs receive a **2xx** response from Jenkins.

With this wiring, creating or updating a PR in Bitbucket DC will trigger the Jenkins job and populate the `SCM_*` parameters expected by the pipeline.

---

## 4. SCM and LLM settings for Bitbucket

Set **environment variables** in Jenkins (job or global) for SCM and LLM configuration:

- **SCM**
  - `SCM_PROVIDER=bitbucket`
  - `SCM_URL`:
    - For Bitbucket **Cloud**: `https://api.bitbucket.org/2.0`
    - For Bitbucket **Data Center / Server**: use your base URL, but note that the current provider is Cloud‑oriented and does not yet support the `/rest/api/1.0/...` endpoints:
      - Example: `https://bitbucket.example.com/rest/api/1.0`
  - `SCM_TOKEN`: Bitbucket token / credentials (injected from Jenkins `SCM_TOKEN` Secret text).

- **LLM**
  - `LLM_PROVIDER`, `LLM_MODEL`: as in `README.md` / `docs/QUICKSTART.md`.
  - API key from Jenkins credentials (e.g. `GOOGLE_API_KEY`).

The pipeline passes these into the `code-review` CLI (inline mode) or into the one‑shot container environment, as documented in `docs/QUICKSTART.md` and `docs/JENKINS-NO-DOCKER.md`.

---

## 5. Current limitations and future work

- The existing `BitbucketProvider` (`src/code_review/providers/bitbucket.py`) is implemented for **Bitbucket Cloud v2.0** (`/2.0/repositories/{workspace}/{repo_slug}/...`).
- Bitbucket Data Center / Server uses a different REST API under `/rest/api/1.0/projects/{projectKey}/repos/{repositorySlug}/...`.
- To have **fully native** Data Center support (diffs, file content, comments, PR metadata) the project will need:
  - A dedicated provider implementation targeting the `/rest/api/1.0` API.
  - Registration of that provider in `get_provider()` with a distinct `SCM_PROVIDER` value (e.g. `bitbucket_dc`).
  - Tests that mock the Bitbucket DC endpoints.

Until that provider exists, this document focuses on wiring Bitbucket Data Center → Jenkins so that:

- Webhooks reliably trigger the pipeline on PR events.
- The PR metadata required by the runner (`owner`, `repo`, `PR number`, `head SHA`) is correctly passed through.

