# Playwright UI tests for Jenkins setup flows

These tests automate Jenkins configuration for the different documented flows (single SCM, multi-SCM, etc.) using a **reusable core** and secrets from a **.env file** (same variable names as Jenkins credential IDs).

**Target Jenkins version: 2.552** (classic UI). Selectors in `core/jenkins.py` are written for this version; other versions may need selector adjustments.

## Setup

1. **Install dependencies** (including Playwright and browsers):

   ```bash
   pip install -e ".[e2e-ui]"
   playwright install chromium
   ```

2. **Create a `.env`** in the repo root (or set env vars) with the secrets Jenkins needs. Variable names must match the credential IDs used in the docs:

   - `SCM_TOKEN` – SCM API token
   - `GOOGLE_API_KEY` – LLM API key

   Optional: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.

3. **Start Jenkins 2.552** (e.g. Docker Compose from Quick Start, or your own instance). Set:

   - `JENKINS_URL` (default: `http://localhost:8080`)
   - `JENKINS_USERNAME` (default: `admin`)
   - `JENKINS_PASSWORD` (default: `admin`)
   - `E2E_UI_REPO_URL` (optional; repo URL for Pipeline script from SCM, e.g. your fork)

## Running the tests

```bash
RUN_E2E_UI=1 pytest tests/e2e_ui/ -m e2e_ui
```

To run with the browser visible:

```bash
E2E_UI_HEADED=1 RUN_E2E_UI=1 pytest tests/e2e_ui/ -m e2e_ui
```

## Structure

- **`core/env_loader.py`** – Loads `.env` and exposes `get_credentials()` (and `get(id)`) so Playwright scripts can use the same variable names as Jenkins credential IDs.
- **`core/jenkins.py`** – **`JenkinsUI`** class with the key actions:
  - `login()`, `create_folder()`, `add_credential_global()`, `add_credential_in_folder()`
  - `set_global_env_vars()`, `create_pipeline_job()`, `configure_webhook_trigger()`
  - `open_job()`, `move_job_into_folder()`
- **`flows/`** – Flow tests that reuse **`JenkinsUI`** and **`EnvLoader`**:
  - `test_single_scm.py` – Single SCM: global creds, global env, one job, webhook.
  - `test_multi_scm.py` – Multi-SCM: one folder + wrapper job per SCM, folder creds.

Selectors are tuned for Jenkins 2.552; if you use a different version, you may need to adjust `core/jenkins.py`.
