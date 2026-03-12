# Running the code-review agent on Jenkins without Docker

Use this when your Jenkins servers (or agents) do **not** have Docker or Podman installed. The pipeline runs the `code-review` CLI directly on the node instead of in a container.

---

## Controller and multiple workers

Jenkins typically has one **controller** and one or more **agents** (workers). The pipeline runs on whichever node the job is assigned to—usually an agent.

**With multiple workers you have two approaches:**

| Approach | What to do |
|----------|------------|
| **Install on all workers** | Install Python + the `code-review` package on every agent that might run the pipeline. The job can run on any node. |
| **Install on some workers** | Install the agent only on selected agents. Add a **label** (e.g. `code-review`) to those agents, then restrict the job to that label so only they run it (see below). |

- **Inline agent (no Docker):** Use one of the approaches above so the CLI is available on the node that runs the job.
- **Container (Docker/Podman):** Each agent that runs the job must have the container runtime and the `code-review-agent` image (build locally or pull from a registry). Again, you can install on all workers or only on labeled ones and restrict the job.

**Restricting the job to specific agents** (when not all workers have the agent):

1. On each agent that has the agent (or image) installed, add a **label** (e.g. `code-review`): **Manage Jenkins** → **Nodes** → select the node → **Configure** → **Labels**.
2. In the pipeline job: **Configure** → **General** → check **Restrict where this project can be run** and set **Label expression** to `code-review` (or your label).

Only nodes with that label will run the pipeline.

---

## 1. Install the agent on each Jenkins node

On every Jenkins **agent** (or controller, if builds run on the controller) that will run the pipeline:

1. **Python 3.10 or newer** must be installed.

2. **Install the code-review-agent package.**  
   This is the Python project in this repo: it provides the `code-review` CLI (the same one that runs inside the Docker image). Installing it on the node lets the pipeline run that CLI directly instead of starting a container. You can install from source (Option A) or from a built wheel/PyPI (Option B).

   **Option A – From the repo (development or CI from source):**

   ```bash
   cd /path/to/code-review   # clone or workspace
   python3 -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -e .
   ```

   Ensure the pipeline runs in an environment where this venv is activated, or that the `code-review` entry point is on `PATH`.

   **Option B – From a wheel (recommended for production):**

   ```bash
   pip install code-review-agent   # from PyPI, if published
   # or
   pip install /path/to/code_review_agent-*.whl
   ```

3. **Verify the CLI:**

   ```bash
   code-review --help
   ```

---

## 2. Configure Jenkins to use the inline agent

1. **Set the inline flag** so the pipeline never tries to run Docker/Podman:
   - In the **job**: **Configure** → **Build Environment** → add **Use secret text(s) or file(s)** or **Inject environment variables** and set:
     - `USE_INLINE_AGENT=true`
   - Or set it globally: **Manage Jenkins** → **System** → **Global properties** → **Environment variables** → add `USE_INLINE_AGENT=true`.

2. **SCM and LLM configuration** (when not using Docker Compose):
   - The inline path reads `SCM_PROVIDER` and `SCM_URL` from the environment. Set them on the job or globally so they match your SCM (e.g. Gitea, GitHub, GitLab).
   - Examples:
     - Gitea: `SCM_PROVIDER=gitea`, `SCM_URL=https://gitea.example.com` (or `http://gitea:3000` if Jenkins can resolve that host).
     - GitHub: `SCM_PROVIDER=github`, `SCM_URL=https://api.github.com`.
   - LLM: set `LLM_PROVIDER`, `LLM_MODEL`, and `LLM_API_KEY` as usual. The pipeline already passes these via parameters or env.

3. **Credentials** (unchanged):
   - **Manage Jenkins** → **Credentials**: create Secret text credentials with IDs `SCM_TOKEN` and `LLM_API_KEY`. The existing Jenkinsfile reads these and passes them into the CLI; the chosen provider is determined by `LLM_PROVIDER`.

4. **Pipeline script**: Use **Pipeline script from SCM** (Script Path `docker/jenkins/Jenkinsfile`) or paste the entire `Jenkinsfile` into **Pipeline script** (inline); the pipeline is self-contained. With `USE_INLINE_AGENT=true`, it will run `code-review --owner ... --repo ... --pr ... --head-sha ...` on the node and will not call Docker/Podman.

---

## 3. Webhook and parameters

- If you use **Generic Webhook Trigger** with Gitea/GitHub/etc., keep the same Post content parameters (`SCM_OWNER`, `SCM_REPO`, `SCM_PR_NUM`, `SCM_HEAD_SHA`, `PR_ACTION`) so the pipeline receives PR data.
- The pipeline validates these and runs:
  `code-review --owner <owner> --repo <repo> --pr <n> --head-sha <sha> --print-findings`

---

## 4. Summary

| Item | With Docker | Without Docker |
|------|-------------|----------------|
| Agent execution | `docker run ... code-review-agent` (or podman) | `code-review --owner ...` on the node |
| Prerequisites on node | Docker or Podman + agent image | Python 3.10+ + installed `code-review` package |
| Jenkins config | (optional) `USE_INLINE_AGENT` for Podman issues | **Set `USE_INLINE_AGENT=true`** |
| SCM / LLM | Often from Compose env | Set **`SCM_PROVIDER`**, **`SCM_URL`** (and LLM vars) in job or global env |

See [QUICKSTART.md](QUICKSTART.md) for Docker-based setup and [DEV_TESTING.md](DEV_TESTING.md) for local CLI usage.
