# Playwright UI tests: automating Jenkins setup

Playwright tests automate Jenkins configuration for the code-review agent so you can drive the same flows as in the docs (single SCM, multi-SCM, etc.) from the command line. They use a **reusable core** and read secrets from a **.env file** (variable names match Jenkins credential IDs).

**Target Jenkins version: 2.552** (classic UI). Selectors in `tests/e2e_ui/core/jenkins.py` are written for this version.

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

Environment variables (optional; defaults shown):

| Variable | Default | Purpose |
|----------|---------|---------|
| `JENKINS_URL` | `http://localhost:8080` | Jenkins base URL |
| `JENKINS_USERNAME` | `admin` | Login user |
| `JENKINS_PASSWORD` | `admin` | Login password |
| `E2E_UI_REPO_URL` | (none) | Repo URL for “Pipeline script from SCM” (e.g. your fork) |

---

## Scenarios and how to run them

Tests are under `tests/e2e_ui/` and only run when **`RUN_E2E_UI=1`** is set.

### 1. Single SCM (global credentials and env, one pipeline job)

**What it does:** Configures Jenkins for one SCM (e.g. Gitea): global credentials (`SCM_TOKEN`, `GOOGLE_API_KEY`), global env vars (`SCM_PROVIDER`, `SCM_URL`), one Pipeline job using `docker/jenkins/Jenkinsfile`, and Generic Webhook Trigger with Gitea-style JSONPath. Matches the flow in [Jenkins (existing installation)](JENKINS-EXISTING.md).

**Run:**

```bash
RUN_E2E_UI=1 pytest tests/e2e_ui/flows/test_single_scm.py -v
```

### 2. Multiple SCMs (one folder + wrapper job per SCM)

**What it does:** Configures Jenkins for two SCMs (Gitea and GitHub): two folders, two Pipeline jobs (using `Jenkinsfile.wrapper-gitea` and `Jenkinsfile.wrapper-github`), folder-scoped credentials from .env, and webhook trigger for each job. No global SCM env. Matches [Jenkins with multiple SCMs](JENKINS-MULTIPLE-SCMS.md).

**Run:**

```bash
RUN_E2E_UI=1 pytest tests/e2e_ui/flows/test_multi_scm.py -v
```

### 3. Run all e2e_ui scenarios

```bash
RUN_E2E_UI=1 pytest tests/e2e_ui/ -m e2e_ui -v
```

### 4. Run with the browser visible

```bash
E2E_UI_HEADED=1 RUN_E2E_UI=1 pytest tests/e2e_ui/ -m e2e_ui -v
```

---

## Summary table

| Scenario | Test path | Command |
|----------|-----------|---------|
| Single SCM (global creds + env, one job) | `tests/e2e_ui/flows/test_single_scm.py` | `RUN_E2E_UI=1 pytest tests/e2e_ui/flows/test_single_scm.py -v` |
| Multiple SCMs (folder + wrapper per SCM) | `tests/e2e_ui/flows/test_multi_scm.py` | `RUN_E2E_UI=1 pytest tests/e2e_ui/flows/test_multi_scm.py -v` |
| All e2e_ui tests | `tests/e2e_ui/` | `RUN_E2E_UI=1 pytest tests/e2e_ui/ -m e2e_ui -v` |

---

## Code structure

- **`tests/e2e_ui/core/env_loader.py`** – Loads `.env` and exposes `get_credentials()` (and `get(id)`) so tests use the same variable names as Jenkins credential IDs.
- **`tests/e2e_ui/core/jenkins.py`** – **`JenkinsUI`** (reusable): `login()`, `create_folder()`, `add_credential_global()`, `add_credential_in_folder()`, `set_global_env_vars()`, `create_pipeline_job()`, `configure_webhook_trigger()`, `open_job()`, `move_job_into_folder()`.
- **`tests/e2e_ui/flows/`** – Scenario tests that call **`JenkinsUI`** and **`EnvLoader`**:
  - `test_single_scm.py` – Single SCM flow above.
  - `test_multi_scm.py` – Multi-SCM flow above.

Selectors in `core/jenkins.py` are tuned for Jenkins 2.552; for other versions you may need to adjust them.
