## GitHub Actions Integration

This guide shows how to run the **code review agent** as a one-shot Docker container in **GitHub Actions** and have it post inline review comments back to a pull request.

This is the recommended GitHub Actions path because:

- it does not require creating a virtualenv on the runner
- it does not require installing Python dependencies in the workflow
- it uses the packaged runtime defined by the agent image
- it keeps the workflow small and predictable

The agent talks to GitHub through the API. It does **not** need to check out the repository to review a PR diff.

---

## 1. What this setup does

At a high level, the workflow:

1. triggers on pull request events
2. passes GitHub PR metadata into the container as `SCM_*` environment variables
3. passes your LLM configuration as `LLM_*` environment variables
4. runs the agent image with the `review` command
5. fetches the PR diff and changed files from GitHub
6. asks the configured LLM to review the diff
7. posts inline comments on the PR for findings that survive filtering and deduplication

The runner also applies a few guardrails automatically:

- skips PRs with a configured skip label or title marker
- avoids reposting duplicate comments for the same PR head SHA
- filters findings to lines that are actually visible in the diff

---

## 2. Requirements

You need:

- a repository hosted on GitHub
- GitHub Actions enabled for that repository or organization
- a published agent image
- an SCM token with permission to read the PR and write PR comments
- an LLM provider and API credential

For most same-repository pull requests:

- use GitHub's built-in `GITHUB_TOKEN` for `SCM_TOKEN`
- use a repository secret such as `LLM_API_KEY` for the model provider

---

## 3. Choose the container image

The workflow can use any registry image built from [`docker/Dockerfile.agent`](../docker/Dockerfile.agent).

Common options:

- a public Docker Hub image such as `e4c5/code-review-agent:latest`
- your own mirrored image, for example `your-org/code-review-agent:latest`
- a pinned release tag such as `your-org/code-review-agent:v1.2.3`

If you publish your own image from this repo, the existing publish workflow is [`publish-agent-image.yml`](../.github/workflows/publish-agent-image.yml).

Recommendation:

- use a pinned version tag in production
- use `latest` only for experimentation

---

## 4. Required secrets and permissions

### 4.1 GitHub token

Set `SCM_TOKEN` to one of:

- `${{ secrets.GITHUB_TOKEN }}` for the simplest same-repo setup
- a PAT stored as a secret such as `${{ secrets.SCM_TOKEN }}`
- a GitHub App installation token generated earlier in the workflow

For inline review comments, the workflow should grant:

```yaml
permissions:
  contents: read
  pull-requests: write
```

If you pull from a private registry such as GHCR, add the registry-specific permissions and login step shown later in this guide.

### 4.2 LLM configuration

The agent needs:

- `LLM_PROVIDER`
- `LLM_MODEL`
- `LLM_API_KEY`

Examples:

- Gemini: `LLM_PROVIDER=gemini`, `LLM_MODEL=gemini-2.5-flash`
- OpenAI: `LLM_PROVIDER=openai`, `LLM_MODEL=gpt-5-mini`
- Anthropic: `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-3-7-sonnet-latest`

Store the API key as a repository or organization secret, for example:

- `LLM_API_KEY`

---

## 5. Minimal workflow using Docker

Create:

- `.github/workflows/code-review.yml`

Example:

```yaml
name: Code Review (AI)

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  packages: read
  pull-requests: write

jobs:
  code-review:
    runs-on: ubuntu-latest

    env:
      IMAGE: e4c5/code-review-agent:latest

      SCM_PROVIDER: github
      SCM_URL: https://api.github.com
      SCM_OWNER: ${{ github.repository_owner }}
      SCM_REPO: ${{ github.event.repository.name }}
      SCM_PR_NUM: ${{ github.event.pull_request.number }}
      SCM_HEAD_SHA: ${{ github.event.pull_request.head.sha }}
      SCM_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      LLM_PROVIDER: gemini
      LLM_MODEL: gemini-2.5-flash
      LLM_API_KEY: ${{ secrets.LLM_API_KEY }}

      CODE_REVIEW_LOG_LEVEL: INFO

    steps:
      - name: Pull agent image
        run: docker pull "$IMAGE"

      - name: Run AI code review
        run: |
          docker run --rm \
            -e SCM_PROVIDER \
            -e SCM_URL \
            -e SCM_OWNER \
            -e SCM_REPO \
            -e SCM_PR_NUM \
            -e SCM_HEAD_SHA \
            -e SCM_TOKEN \
            -e LLM_PROVIDER \
            -e LLM_MODEL \
            -e LLM_API_KEY \
            -e CODE_REVIEW_LOG_LEVEL \
            "$IMAGE" \
            review \
            --owner "$SCM_OWNER" \
            --repo "$SCM_REPO" \
            --pr "$SCM_PR_NUM" \
            --head-sha "$SCM_HEAD_SHA"
```

Notes:

- no `actions/checkout` step is required
- no Python setup is required
- the container already has `code-review` as its entrypoint
- the explicit `review` subcommand is still required

---

## 6. Recommended production workflow

For production use, prefer:

- a pinned image tag
- same-repo pull requests only for the initial rollout
- skipping draft pull requests
- explicit branch filters
- concurrency cancellation
- a job timeout
- stable logging defaults

This example is a good starting point for a live repository when you want the agent to post comments on trusted, same-repo pull requests without taking on fork-PR risk yet.

It assumes you are using the public Docker Hub image published by [`publish-agent-image.yml`](../.github/workflows/publish-agent-image.yml). That workflow pushes to the repository named by `DOCKERHUB_REPO`, or falls back to `${DOCKERHUB_USERNAME}/code-review-agent`.

Example:

```yaml
name: Code Review (AI)

on:
  pull_request:
    branches:
      - main
      - release/*
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: code-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  code-review:
    name: Review pull request
    runs-on: ubuntu-latest
    timeout-minutes: 20
    if: >
      github.event.pull_request.draft == false &&
      github.event.pull_request.head.repo.full_name == github.repository

    env:
      IMAGE: your-dockerhub-user/code-review-agent:v1.2.3

      SCM_PROVIDER: github
      SCM_URL: https://api.github.com
      SCM_OWNER: ${{ github.repository_owner }}
      SCM_REPO: ${{ github.event.repository.name }}
      SCM_PR_NUM: ${{ github.event.pull_request.number }}
      SCM_HEAD_SHA: ${{ github.event.pull_request.head.sha }}
      SCM_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      LLM_PROVIDER: openai
      LLM_MODEL: gpt-5-mini
      LLM_API_KEY: ${{ secrets.LLM_API_KEY }}

      SCM_SKIP_LABEL: skip-ai-review
      SCM_SKIP_TITLE_PATTERN: "[skip-ai-review]"
      CODE_REVIEW_LOG_LEVEL: INFO

    steps:
      - name: Validate required configuration
        run: |
          test -n "$LLM_API_KEY" || { echo "LLM_API_KEY is not set"; exit 1; }

      - name: Pull agent image
        run: docker pull "$IMAGE"

      - name: Run AI code review
        run: |
          docker run --rm \
            -e SCM_PROVIDER \
            -e SCM_URL \
            -e SCM_OWNER \
            -e SCM_REPO \
            -e SCM_PR_NUM \
            -e SCM_HEAD_SHA \
            -e SCM_TOKEN \
            -e SCM_SKIP_LABEL \
            -e SCM_SKIP_TITLE_PATTERN \
            -e LLM_PROVIDER \
            -e LLM_MODEL \
            -e LLM_API_KEY \
            -e CODE_REVIEW_LOG_LEVEL \
            "$IMAGE" \
            review \
            --owner "$SCM_OWNER" \
            --repo "$SCM_REPO" \
            --pr "$SCM_PR_NUM" \
            --head-sha "$SCM_HEAD_SHA"
```

Notes:

- this intentionally skips fork-originated pull requests
- this intentionally skips draft pull requests until they are marked ready for review
- no `actions/checkout` step is required because the agent reads PR data from the GitHub API
- set `IMAGE` to the exact Docker Hub repository and tag your publish workflow produced
- `GITHUB_TOKEN` is the simplest starting token for same-repo PRs; switch to a PAT or GitHub App token only if your org policy requires it
- keep `SCM_REVIEW_DECISION_ENABLED` off at first unless you are ready to automate `APPROVE` / `REQUEST_CHANGES`

---

## 7. Optional workflow variants

### 7.1 Dry run

Use this first if you want to validate configuration without posting comments:

```yaml
- name: Run AI code review (dry run)
  run: |
    docker run --rm \
      -e SCM_PROVIDER \
      -e SCM_URL \
      -e SCM_OWNER \
      -e SCM_REPO \
      -e SCM_PR_NUM \
      -e SCM_HEAD_SHA \
      -e SCM_TOKEN \
      -e LLM_PROVIDER \
      -e LLM_MODEL \
      -e LLM_API_KEY \
      -e CODE_REVIEW_LOG_LEVEL \
      "$IMAGE" \
      review \
      --owner "$SCM_OWNER" \
      --repo "$SCM_REPO" \
      --pr "$SCM_PR_NUM" \
      --head-sha "$SCM_HEAD_SHA" \
      --dry-run
```

### 7.2 Fail the job on critical findings

Use this only if you want the review to become merge-blocking:

```yaml
- name: Run AI code review (fail on critical)
  run: |
    docker run --rm \
      -e SCM_PROVIDER \
      -e SCM_URL \
      -e SCM_OWNER \
      -e SCM_REPO \
      -e SCM_PR_NUM \
      -e SCM_HEAD_SHA \
      -e SCM_TOKEN \
      -e LLM_PROVIDER \
      -e LLM_MODEL \
      -e LLM_API_KEY \
      -e CODE_REVIEW_LOG_LEVEL \
      "$IMAGE" \
      review \
      --owner "$SCM_OWNER" \
      --repo "$SCM_REPO" \
      --pr "$SCM_PR_NUM" \
      --head-sha "$SCM_HEAD_SHA" \
      --fail-on-critical
```

The CLI exits with status `2` if any finding has severity `high`.

### 7.3 Use a PAT instead of `GITHUB_TOKEN`

Replace:

```yaml
SCM_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

with:

```yaml
SCM_TOKEN: ${{ secrets.SCM_TOKEN }}
```

This is useful if:

- your org restricts the default token
- you need cross-repository access
- you want token management outside the workflow permission model

### 7.4 Pull from a private registry

If your image is not public, log in before `docker pull`.

Private GHCR example:

```yaml
permissions:
  contents: read
  packages: read
  pull-requests: write

steps:
  - name: Log in to GHCR
    uses: docker/login-action@v3
    with:
      registry: ghcr.io
      username: ${{ github.actor }}
      password: ${{ secrets.GITHUB_TOKEN }}
```

Use this when you mirror the agent image to a private GHCR package instead of pulling the public Docker Hub image.

Private Docker Hub example:

```yaml
- name: Log in to Docker Hub
  uses: docker/login-action@v3
  with:
    username: ${{ secrets.DOCKERHUB_USERNAME }}
    password: ${{ secrets.DOCKERHUB_TOKEN }}
```

### 7.5 Comment-triggered review decision refresh

To re-run **only** the quality gate and PR review decision after discussion activity (no LLM), trigger a workflow on events such as `issue_comment` or `pull_request_review_comment` and run the agent with `--review-decision-only` plus `SCM_REVIEW_DECISION_ENABLED=true`. `SCM_HEAD_SHA` may be empty; the runner resolves the current head from the GitHub API when needed.

Optional `CODE_REVIEW_EVENT_*` variables add structured audit fields to logs; see [Configuration reference — §5.1](CONFIGURATION-REFERENCE.md).

Example container invocation (adapt `SCM_PR_NUM` / `SCM_HEAD_SHA` to your event shape):

```yaml
- run: |
    docker run --rm \
      -e SCM_PROVIDER -e SCM_URL -e SCM_TOKEN \
      -e SCM_OWNER -e SCM_REPO -e SCM_PR_NUM \
      -e SCM_REVIEW_DECISION_ENABLED=true \
      "$IMAGE" review \
      --owner "$SCM_OWNER" --repo "$SCM_REPO" --pr "$SCM_PR_NUM" \
      --review-decision-only
```

---

## 8. What the agent posts on GitHub

On GitHub, the normal success path posts:

- inline review comments on diff lines for findings

It may also post a PR-level note at the start of the run if the PR description is empty or too short and the runner auto-generates context.

It does **not** rely on a final catch-all summary comment for normal GitHub review posting.

---

## 9. Does this work for every pull request?

Not always.

### 9.1 Same-repo PRs

This is the easiest and most reliable case.

If the workflow has:

- `pull-requests: write`
- access to `LLM_API_KEY`
- a valid image tag

then same-repo PRs are the best fit for this setup.

### 9.2 Fork PRs

Fork PRs are the main exception.

With a normal `pull_request` workflow:

- repository secrets are typically not exposed to untrusted fork PR runs
- the default `GITHUB_TOKEN` is typically restricted for fork-originated PRs

That means the simple workflow above usually does **not** work for untrusted fork PRs because:

- the LLM secret is unavailable
- PR comment write access may be unavailable

If fork PR support matters, use a different design with care, such as:

- `pull_request_target` with very strict hardening
- an external review service
- manual approval or maintainer-gated execution

Do not switch to `pull_request_target` casually. It has a different security model and must not execute untrusted PR code.

The production example above avoids this by explicitly running only for same-repo pull requests.

### 9.3 Intentionally skipped PRs

The runner can skip a PR when:

- it has the configured skip label
- its title contains the configured skip pattern

Default values are:

- `skip-review`
- `[skip-review]`

---

## 10. How the review flow works internally

When the container runs `code-review review`, the agent:

1. validates `owner`, `repo`, `pr`, and `head_sha`
2. loads SCM and LLM configuration from the environment
3. connects to the GitHub provider
4. checks whether the PR should be skipped
5. loads existing review comments for deduplication
6. computes idempotency for the current PR head SHA and config
7. fetches changed files and the unified diff from GitHub
8. chooses single-shot or file-by-file review based on diff size
9. runs the review model
10. filters findings to valid diff-visible lines
11. removes duplicates and already-resolved items
12. posts inline review comments back to the PR

This behavior is what makes reruns safe: the workflow can be retried without blindly reposting the same comments.

---

## 11. Verifying the integration

After you add the workflow:

1. open or update a pull request
2. open the PR's **Checks** tab
3. confirm the workflow runs
4. inspect the log output for messages such as:
   - `Fetched diff`
   - `Running agent`
   - `Agent returned`
5. open the PR **Files changed** tab and confirm inline comments appear

If you see no comments, check:

- `SCM_TOKEN` can write pull request comments
- `LLM_API_KEY` exists and matches `LLM_PROVIDER`
- `SCM_HEAD_SHA` is populated from `github.event.pull_request.head.sha`
- the image tag exists and can be pulled
- the PR was not skipped by label or title pattern

---

## 12. Troubleshooting

### The workflow runs but no comments appear

Common causes:

- `SCM_TOKEN` lacks `pull-requests: write`
- the run was a dry run
- the PR matched the skip label or skip title pattern
- the model returned no valid findings after filtering
- the findings pointed to lines outside the visible diff hunks

### The workflow cannot access `LLM_API_KEY`

Most often this is a fork PR restriction. See the fork PR notes above.

### The workflow cannot pull the image

Check:

- the image name and tag
- registry credentials if the image is private
- whether the tag was actually published

### Comments duplicate on reruns

This should be rare. The runner uses idempotency markers and existing-comment fingerprinting. If duplicates appear, check whether:

- the head SHA changed between runs
- the model or provider config changed
- old comments were deleted manually

---

## 13. Recommended defaults

For most teams:

- trigger on `opened`, `synchronize`, `reopened`, and `ready_for_review`
- use `GITHUB_TOKEN` first
- use a pinned container tag
- set `CODE_REVIEW_LOG_LEVEL=INFO`
- skip fork PRs in the first live rollout
- start with advisory comments only
- add `--fail-on-critical` only after the signal quality is proven

---

## 14. Reference workflow checklist

Before rolling this out broadly, verify:

- the image is published and pullable
- `LLM_API_KEY` is configured
- `permissions.pull-requests` is set to `write`
- `SCM_HEAD_SHA` comes from `${{ github.event.pull_request.head.sha }}`
- the workflow is tested on a same-repo PR first
- the initial live workflow skips fork PRs unless you have a hardened design for them
- your team has an explicit policy for fork PR behavior
