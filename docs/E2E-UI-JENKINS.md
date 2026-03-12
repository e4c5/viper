# Playwright: standalone Jenkins setup scripts

Standalone Playwright scripts automate Jenkins configuration for **viper** so you can drive the same flows as in the docs (single SCM, multi-SCM) from the command line. They use a **reusable core** and read secrets from a **.env file** (variable names match Jenkins credential IDs).

**Not part of the test suite or CI.** Run these scripts manually when you want to visually confirm or automate Jenkins setup.

**Target Jenkins version: 2.552** (classic UI). Selectors in `e2e_ui/core/jenkins.py` are written for this version.

---

## Prerequisites

1. **Jenkins 2.552** running (e.g. via [Quick Start](QUICKSTART.md) Docker Compose, or your own instance).
2. **.env** in the repo root. Copy from `.env.example` and set at least:
   - **Credentials** (same names as Jenkins credential IDs): **`SCM_TOKEN`** and **`LLM_API_KEY`** are required for the documented review flow to succeed. The LLM provider is determined by `LLM_PROVIDER` (e.g. gemini, openai, openrouter).
   - **E2E UI variables** listed below (required/optional per script).

---

## Setup (one-time)

```bash
pip install -e ".[e2e-ui]"
playwright install chromium
```

---

## Environment variables (all in .env)

All configuration for the Playwright scripts is via environment variables (or `.env`). No URLs or provider names are hardcoded in the scripts. See `.env.example` for a template.

### Required for both scripts

| Variable | Purpose |
|----------|---------|
| `JENKINS_USERNAME` | Jenkins login user (e.g. `admin` for local Docker). |
| `JENKINS_PASSWORD` | Jenkins login password. |
| `E2E_UI_REPO_URL` | Repo URL for “Pipeline script from SCM”. Set to the repo Jenkins will clone (e.g. your fork). Use HTTPS in production. |

Credentials used by the scripts (and written into Jenkins) come from the same .env; variable names match Jenkins credential IDs. **`SCM_TOKEN`** and **`LLM_API_KEY`** are required for the documented review flow to succeed; set **`LLM_PROVIDER`** and **`LLM_MODEL`** as needed.

### Required only for `run_single_scm`

| Variable | Purpose |
|----------|---------|
| `SCM_PROVIDER` | SCM identifier written to Jenkins global env (e.g. `gitea`, `github`, `gitlab`, `bitbucket`). Must match viper. |
| `SCM_URL` | SCM base URL written to Jenkins global env. **Use HTTPS in production**; clear-text HTTP is acceptable only for local/dev (e.g. Docker `http://gitea:3000`). |

### Optional (sensible defaults)

| Variable | Default | Purpose |
|----------|---------|---------|
| `JENKINS_URL` | `http://localhost:8080` | Jenkins base URL (no trailing slash). Used only for local runs when unset. |
| `E2E_UI_BRANCH` | `main` | Branch for “Pipeline script from SCM”. |
| `E2E_UI_HEADED` | (unset = headless) | Set to `1` to run the browser visible (e.g. `E2E_UI_HEADED=1`). |

Set these in `.env` in the repo root (or export them). The scripts load `.env` via the same mechanism as the credential IDs; see `.env.example` for a full commented list.

---

## Scenarios and how to run them

Run from the **repo root** so the `e2e_ui` package and paths (e.g. `docker/jenkins/Jenkinsfile`) resolve correctly.

### 1. Single SCM (global credentials and env, one pipeline job)

**What it does:** Configures Jenkins for one SCM (e.g. Gitea): global credentials (`SCM_TOKEN`, `LLM_API_KEY`), global env vars (`SCM_PROVIDER`, `SCM_URL`), one Pipeline job using `docker/jenkins/Jenkinsfile`, and Generic Webhook Trigger with Gitea-style JSONPath. Matches the flow in [Jenkins (existing installation)](JENKINS-EXISTING.md).

**Run:**

```bash
python -m e2e_ui.run_single_scm
```

### 2. Multiple SCMs (one folder + job per SCM)

**What it does:** Configures Jenkins with two folders and two Pipeline jobs, each using **Script Path** `docker/jenkins/Jenkinsfile`, folder-scoped credentials from .env, and webhook trigger. For **different** SCMs (e.g. Gitea vs GitHub), set each job’s **parameter defaults** for `SCM_PROVIDER` and `SCM_URL` in job → Configure → Parameters—see [Jenkins with multiple SCMs](JENKINS-MULTIPLE-SCMS.md). The script does not set those defaults; it sets up the folder/job structure and credentials.

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
| Multiple SCMs (folder + job per SCM) | `python -m e2e_ui.run_multi_scm` |

---

## Code structure

- **`e2e_ui/core/env_loader.py`** – Loads `.env` and exposes `get_credentials()` (and `get(id)`) so scripts use the same variable names as Jenkins credential IDs.
- **`e2e_ui/core/jenkins.py`** – **`JenkinsUI`** (reusable): `login()`, `create_folder()`, `add_credential_global()`, `add_credential_in_folder()`, `set_global_env_vars()`, `create_pipeline_job()`, `configure_webhook_trigger()`, `open_job()`, `move_job_into_folder()`.
- **`e2e_ui/core/runner.py`** – **Shared setup for all Playwright scripts**: env validation (`get_jenkins_config`, `get_credentials`, `get_repo_and_branch`, `require_scm_env`), `GITEA_WEBHOOK_PARAMS`, and `jenkins_session()` context manager (Playwright + logged-in JenkinsUI). The run_*.py scripts depend on this module and only implement their flow-specific steps.
- **`e2e_ui/run_single_scm.py`** – Standalone script for the single-SCM flow (uses runner for setup and session).
- **`e2e_ui/run_multi_scm.py`** – Standalone script for the multi-SCM flow (uses runner for setup and session).

Selectors in `e2e_ui/core/jenkins.py` are tuned for Jenkins 2.552; for other versions you may need to adjust them.
