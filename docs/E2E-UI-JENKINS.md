# Playwright: standalone Jenkins setup scripts

Standalone Playwright scripts automate Jenkins configuration for the code-review agent so you can drive the same flows as in the docs (single SCM, multi-SCM) from the command line. They use a **reusable core** and read secrets from a **.env file** (variable names match Jenkins credential IDs).

**Not part of the test suite or CI.** Run these scripts manually when you want to visually confirm or automate Jenkins setup.

**Target Jenkins version: 2.552** (classic UI). Selectors in `e2e_ui/core/jenkins.py` are written for this version.

---

## Prerequisites

1. **Jenkins 2.552** running (e.g. via [Quick Start](QUICKSTART.md) Docker Compose, or your own instance).
2. **.env** in the repo root with the same variable names as Jenkins credential IDs:
   - `SCM_TOKEN` – SCM API token
   - `GOOGLE_API_KEY` – LLM API key  
   Optional: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.

---

## Setup (one-time)

```bash
pip install -e ".[e2e-ui]"
playwright install chromium
```

Environment variables:

| Variable | Required | Purpose |
|----------|----------|---------|
| `JENKINS_URL` | No (default: `http://localhost:8080`) | Jenkins base URL |
| `JENKINS_USERNAME` | **Yes** | Jenkins login user (e.g. `admin` for local Docker) |
| `JENKINS_PASSWORD` | **Yes** | Jenkins login password (e.g. `admin` for local Docker) |
| `E2E_UI_REPO_URL` | No | Repo URL for “Pipeline script from SCM” (e.g. your fork) |

Set `JENKINS_USERNAME` and `JENKINS_PASSWORD` in the environment or in `.env` before running the scripts.

---

## Scenarios and how to run them

Run from the **repo root** so the `e2e_ui` package and paths (e.g. `docker/jenkins/Jenkinsfile`) resolve correctly.

### 1. Single SCM (global credentials and env, one pipeline job)

**What it does:** Configures Jenkins for one SCM (e.g. Gitea): global credentials (`SCM_TOKEN`, `GOOGLE_API_KEY`), global env vars (`SCM_PROVIDER`, `SCM_URL`), one Pipeline job using `docker/jenkins/Jenkinsfile`, and Generic Webhook Trigger with Gitea-style JSONPath. Matches the flow in [Jenkins (existing installation)](JENKINS-EXISTING.md).

**Run:**

```bash
python -m e2e_ui.run_single_scm
```

### 2. Multiple SCMs (one folder + wrapper job per SCM)

**What it does:** Configures Jenkins for two SCMs (Gitea and GitHub): two folders, two Pipeline jobs (each using **Script Path** `docker/jenkins/Jenkinsfile.multi-scm-wrapper`), folder-scoped credentials from .env, and webhook trigger for each job. No global SCM env. Matches [Jenkins with multiple SCMs](JENKINS-MULTIPLE-SCMS.md).

**Run:**

```bash
python -m e2e_ui.run_multi_scm
```

### 3. Run with the browser visible

By default the browser runs headless. To watch the flow:

```bash
E2E_UI_HEADED=1 python -m e2e_ui.run_single_scm
E2E_UI_HEADED=1 python -m e2e_ui.run_multi_scm
```

---

## Summary table

| Scenario | Command |
|----------|---------|
| Single SCM (global creds + env, one job) | `python -m e2e_ui.run_single_scm` |
| Multiple SCMs (folder + wrapper per SCM) | `python -m e2e_ui.run_multi_scm` |

---

## Code structure

- **`e2e_ui/core/env_loader.py`** – Loads `.env` and exposes `get_credentials()` (and `get(id)`) so scripts use the same variable names as Jenkins credential IDs.
- **`e2e_ui/core/jenkins.py`** – **`JenkinsUI`** (reusable): `login()`, `create_folder()`, `add_credential_global()`, `add_credential_in_folder()`, `set_global_env_vars()`, `create_pipeline_job()`, `configure_webhook_trigger()`, `open_job()`, `move_job_into_folder()`.
- **`e2e_ui/run_single_scm.py`** – Standalone script for the single-SCM flow.
- **`e2e_ui/run_multi_scm.py`** – Standalone script for the multi-SCM flow.

Selectors in `e2e_ui/core/jenkins.py` are tuned for Jenkins 2.552; for other versions you may need to adjust them.
