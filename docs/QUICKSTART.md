# Quick Start Guide

Get the code review agent running with Docker Compose (or Podman Compose), Gitea, Jenkins, and auto-triggered PR reviews.

---

## Prerequisites

- **Docker** and **Docker Compose**, **or** **Podman** and **Podman Compose**
- **LLM API key** (for example `GOOGLE_API_KEY`)

To run **Jenkins without Docker** (no containers on the agent), see [JENKINS-NO-DOCKER.md](JENKINS-NO-DOCKER.md).

---

## 1. Start the stack (Docker)

From the **repository root** (the folder that contains `docker-compose.yml`):

**Docker:**

```bash
docker compose up -d --build
```

- **Gitea**: http://localhost:3000  
- **Jenkins**: http://localhost:8080  

For **Podman**, see `docs/QUICKSTART-podman.md` for rootless setup, inline mode, and troubleshooting.

After changing the Jenkins image or Compose file, rebuild and restart the stack:

**Docker:**

```bash
docker compose down
docker compose up -d --build
```

## 2. Configure Gitea

1. Open http://localhost:3000 and complete first-run setup (admin user, etc.).
2. Create a **repository** (e.g. `myrepo`) under a user or org (e.g. `myorg`).
3. Create an **API token**: **Settings → Applications → Generate New Token**.

---

## 3. Configure Jenkins

1. Open http://localhost:8080. Default credentials are `admin` / `admin` (from `docker-compose.yml`).
2. **Add credentials**:
   - Go to **Manage Jenkins → Credentials → System → Global credentials (unrestricted)**.
   - **Add Credentials** → Kind: **Secret text**.
   - Create:
     - ID: `SCM_TOKEN`, Secret: your Gitea API token.
     - ID: `GOOGLE_API_KEY` (or `OPENAI_API_KEY`, etc.), Secret: your LLM API key.
3. Create a **Pipeline** job:
   - Click **New Item** (left nav) or **Create a job** (home page), then choose **Pipeline**.
   - **Pipeline script from SCM** → point to this repo and set **Script Path** to `docker/jenkins/Jenkinsfile`,  
     **or** use **Pipeline script** and paste the contents of `docker/jenkins/Jenkinsfile`.
   - Do **not** add `SCM_*` parameters in the Jenkins UI when using **Pipeline script from SCM**.

**How values are provided**
- `SCM_TOKEN` and `GOOGLE_API_KEY` come from Jenkins **Credentials**.
- `SCM_OWNER`, `SCM_REPO`, `SCM_PR_NUM`, and `SCM_HEAD_SHA` come from the webhook trigger mappings below.
- `.env` is only used by Docker Compose to substitute values in `docker-compose.yml`.

---

## 4. Build or pull the agent image

You can either pull a prebuilt image from Docker Hub or build the image locally from this repository.

**Option A – Pull prebuilt image (recommended for CI):**

```bash
docker pull e4c5/code-review-agent
docker tag e4c5/code-review-agent code-review-agent
```

The second command tags the image as `code-review-agent` so the existing `docker-compose.yml` and Jenkins examples continue to work without changes.

**Option B – Build locally:**

From the repository root:

**Docker:**

```bash
docker build -t code-review-agent -f docker/Dockerfile.agent .
```

**Podman:**

```bash
podman build -t code-review-agent -f docker/Dockerfile.agent .
```

### When to rebuild (and why it’s fast)

- **Rebuild required** whenever you change code under `src/` or runtime dependencies in `pyproject.toml`, so that the `code-review-agent` image picks up those changes.
- The `docker/Dockerfile.agent` is structured so that dependency metadata (`pyproject.toml`) is copied before the source tree; Docker can therefore **cache the expensive `pip install` layer**, and most code-only edits only invalidate the final layers, keeping rebuilds relatively fast.

---

## 5. Auto-trigger PR reviews (Gitea webhook → Jenkins)

This section applies to **Gitea** (and GitHub-style) webhooks. For **Bitbucket Data Center**, use a separate pipeline and see [Bitbucket Data Center](BITBUCKET-DATACENTER.md).

### 5.1 Configure the Jenkins webhook trigger

The Jenkins image already includes **Generic Webhook Trigger**.

Open your Pipeline job → **Configure**.

In **Build Triggers**:
- Check **Generic Webhook Trigger**.
- In the plugin section, find **Post content parameters**.
- Add these 5 variables there:

- `SCM_OWNER` → `$.pull_request.base.repo.owner.login`
- `SCM_REPO` → `$.pull_request.base.repo.name`
- `SCM_PR_NUM` → `$.pull_request.number`
- `SCM_HEAD_SHA` → `$.pull_request.head.sha`
- `PR_ACTION` → `$.action`

1. For each variable, set:
   `Variable` = the name above
   `Expression` = the matching JSONPath above
   `Expression type` = `JSONPath`
2. In the same **Generic Webhook Trigger** section, set:
   `Optional filter` text = `$PR_ACTION`
   `Optional filter` regexp = `^(opened|synchronize)$`
3. Save the job.
4. Re-open the job configuration if needed and copy the **Webhook URL** shown in the same **Generic Webhook Trigger** section.

The pipeline also checks `PR_ACTION` itself and skips execution unless the action is `opened` or `synchronize`.

### 5.2 Configure the Gitea webhook (repo-level)

1. In Gitea, open your repo → **Settings → Webhooks → Add Webhook → Gitea**.
2. **Target URL**: paste the Jenkins webhook URL from step 5.1.
3. **Content Type**: `application/json`.
4. **Trigger On**: **Pull Request**.
5. Save the webhook.
6. Confirm **Delivery History** shows a 2xx response.

If delivery fails with `webhook can only call allowed HTTP servers`, verify [docker-compose.yml](../docker-compose.yml) includes `GITEA__webhook__ALLOWED_HOST_LIST: jenkins,jenkins:8080`.

Now, when a PR is opened or updated, Jenkins will trigger the pipeline and run the review.

---

## Next steps

For development workflows beyond this setup, see [docs/DEV_TESTING.md](./DEV_TESTING.md).
