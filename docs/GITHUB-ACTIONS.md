## GitHub Actions Integration

This guide explains how to run the **code review agent** on every pull request using **GitHub Actions** with the built‑in `GITHUB_TOKEN` (or a PAT / GitHub App token) and post inline comments back to the PR.

The goal is:

- **Trigger**: On `pull_request` events (opened / synchronized / reopened / ready_for_review).
- **Run**: The `code-review` CLI once per PR head commit.
- **Post**: Inline comments and an optional summary using the GitHub provider.

---

## 1. Requirements

- **Repository** hosted on GitHub.
- **GitHub Actions enabled** for the repository or organization.
- **LLM provider credentials**:
  - One of: Gemini, OpenAI, Anthropic, Vertex, Ollama, OpenRouter.
  - An API key stored as a GitHub Actions secret (for example `LLM_API_KEY`).
- **Python environment** (recommended) or Docker if you prefer the container image.

You do *not* need to run any long‑lived service: the agent runs as a one‑shot job inside the workflow.

---

## 2. Environment variables (recap)

The agent reads configuration from environment variables; it does **not** auto‑load `.env` files.

- **SCM (GitHub)**
  - `SCM_PROVIDER=github`
  - `SCM_URL=https://api.github.com`
  - `SCM_TOKEN` – token used for GitHub API calls.
  - `SCM_OWNER` – repository owner (user or organization).
  - `SCM_REPO` – repository name.
  - `SCM_PR_NUM` – pull request number.
  - `SCM_HEAD_SHA` – head commit SHA of the PR (required when posting comments).
  - Optional: `SCM_SKIP_LABEL`, `SCM_SKIP_TITLE_PATTERN` to skip certain PRs.

- **LLM**
  - `LLM_PROVIDER` – e.g. `gemini` (default), `openai`, `anthropic`, `vertex`, `ollama`, `openrouter`.
  - `LLM_MODEL` – e.g. `gemini-2.5-flash`.
  - `LLM_API_KEY` – API key for the selected LLM provider.

The CLI can also receive `--owner`, `--repo`, `--pr`, `--head-sha` flags, but in GitHub Actions it is usually simpler to set the `SCM_*` env vars.

---

## 3. Choosing an authentication method for GitHub

You have three main options for `SCM_TOKEN`:

- **Default `GITHUB_TOKEN` (recommended for most repos)**:
  - GitHub automatically injects a short‑lived token in each workflow run.
  - Works well if your workflow’s `permissions` grant `pull-requests: write` (for inline comments).
  - Use: `SCM_TOKEN: ${{ secrets.GITHUB_TOKEN }}`.

- **Personal Access Token (PAT)**:
  - Create a fine‑grained PAT with “Pull requests: read/write” on the target repo or org.
  - Store it as a **repository secret** (e.g. `SCM_TOKEN`).
  - Use when you need cross‑repo access or more control than `GITHUB_TOKEN`.

- **GitHub App installation token**:
  - Create a GitHub App with `Pull requests: read/write` and `Contents: read`.
  - Your CI (or a pre‑step) exchanges the App credentials for an installation token and sets `SCM_TOKEN`.
  - Best for org‑wide, multi‑repo deployments with least‑privilege access.

For an initial setup, using the built‑in `GITHUB_TOKEN` is sufficient and simplest.

---

## 4. Minimal GitHub Actions workflow (Python CLI)

Create a workflow file in your repository at:

- `.github/workflows/code-review.yml`

### 4.1 Example workflow

```yaml
name: Code Review (AI)

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write

jobs:
  code-review:
    runs-on: ubuntu-latest

    env:
      # SCM (GitHub) configuration
      SCM_PROVIDER: github
      SCM_URL: https://api.github.com
      SCM_OWNER: ${{ github.repository_owner }}
      SCM_REPO: ${{ github.event.repository.name }}
      SCM_PR_NUM: ${{ github.event.pull_request.number }}
      SCM_HEAD_SHA: ${{ github.event.pull_request.head.sha }}

      # Token used by the GitHub provider
      SCM_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      # LLM configuration (example: Gemini)
      LLM_PROVIDER: gemini
      LLM_MODEL: gemini-2.5-flash
      LLM_API_KEY: ${{ secrets.LLM_API_KEY }}

      # Optional: make the agent more verbose in logs
      CODE_REVIEW_LOG_LEVEL: INFO

    steps:
      - name: Checkout code
        uses: actions/checkout@v4
        with:
          # Ensure we have the PR head commit
          ref: ${{ github.event.pull_request.head.sha }}

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install code-review-agent
        run: |
          python -m pip install --upgrade pip
          pip install "code-review-agent"  # or pip install . if running from this repo

      - name: Run AI code review
        run: |
          code-review --owner "$SCM_OWNER" --repo "$SCM_REPO" --pr "$SCM_PR_NUM" --head-sha "$SCM_HEAD_SHA"
```

This workflow:

- Runs on every relevant PR event.
- Uses the GitHub provider with the built‑in `GITHUB_TOKEN`.
- Calls the CLI once per run and posts inline comments + a summary on the PR.

---

## 5. Optional: use dry‑run or fail‑on‑critical

The CLI supports extra flags that you can add to the workflow:

- **Dry run (no comments posted)**:

  ```yaml
  - name: Run AI code review (dry run)
    run: |
      code-review --owner "$SCM_OWNER" --repo "$SCM_REPO" --pr "$SCM_PR_NUM" --head-sha "$SCM_HEAD_SHA" --dry-run --print-findings
  ```

  - The agent parses findings and prints them to the log but does not post comments.
  - Useful while validating configuration.

- **Fail the job on critical findings**:

  ```yaml
  - name: Run AI code review (block on critical)
    run: |
      code-review --owner "$SCM_OWNER" --repo "$SCM_REPO" --pr "$SCM_PR_NUM" --head-sha "$SCM_HEAD_SHA" --fail-on-critical
  ```

  - The CLI exits with status `2` when any finding has severity `"critical"`.
  - You can mark this job as a required check to block merges on critical issues.

You can also combine flags (for example `--dry-run --print-findings` or `--fail-on-critical --print-findings`).

---

## 6. Using a PAT or GitHub App token instead of `GITHUB_TOKEN`

If you prefer not to rely on `GITHUB_TOKEN`:

- **PAT**:
  - Create a fine‑grained PAT with:
    - `contents: read`
    - `pull_requests: read/write` (or equivalent).
  - Store it as a secret, e.g. `SCM_TOKEN`.
  - Update the workflow:

    ```yaml
    env:
      SCM_TOKEN: ${{ secrets.SCM_TOKEN }}
    ```

- **GitHub App token**:
  - Have a previous step in the workflow that exchanges the App’s credentials for an **installation access token** and writes it to `$GITHUB_ENV`:

    ```yaml
    - name: Generate GitHub App token
      run: |
        echo "SCM_TOKEN=${INSTALLATION_TOKEN_FROM_APP}" >> "$GITHUB_ENV"
    ```

  - The `code-review` step can then use `SCM_TOKEN` from the environment as usual.

The rest of the configuration remains identical (`SCM_PROVIDER=github`, `SCM_URL=https://api.github.com`, etc.).

---

## 7. Customizing triggers and behavior

- **Only run on certain branches**:

  ```yaml
  on:
    pull_request:
      branches:
        - main
        - release/*
  ```

- **Skip certain PRs automatically**:
  - Use `SCM_SKIP_LABEL` (default `skip-review`) and/or `SCM_SKIP_TITLE_PATTERN` (default `[skip-review]`).
  - Example:

    ```yaml
    env:
      SCM_SKIP_LABEL: skip-ai-review
      SCM_SKIP_TITLE_PATTERN: "[skip-ai-review]"
    ```

  - If a PR has the label or its title contains the substring, the runner logs a message and returns without posting comments.

- **Adjust logging**:
  - `CODE_REVIEW_LOG_LEVEL=INFO` for progress logs, `DEBUG` for verbose output.

---

## 8. Verifying the integration

After pushing the workflow file:

1. Open a new pull request (or update an existing one).
2. In the PR’s **Checks** tab, confirm that the **“Code Review (AI)”** workflow runs.
3. Open the job logs:
   - Look for messages such as “Fetched diff”, “Agent returned N finding(s)”.
4. Return to the **Conversation** and **Files changed** tabs:
   - Inline comments from the agent should appear on the modified lines.
   - A summary comment should appear if there were findings to report.

If the job fails or posts no comments, check:

- That `LLM_PROVIDER`, `LLM_MODEL`, and `LLM_API_KEY` are set correctly.
- That `SCM_TOKEN` has permission to read the repo and write PR comments.
- That `SCM_HEAD_SHA` is set to the PR head SHA (required to post comments).

Once things look good, you can tighten conditions (e.g. run only on `main`), enable `--fail-on-critical`, or tweak review prompts via the `standards/prompts` configuration.

