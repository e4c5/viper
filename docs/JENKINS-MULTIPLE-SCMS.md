# Running the code-review agent with multiple SCMs

If your team uses **more than one SCM** (e.g. Gitea and GitHub, or GitHub and Bitbucket Data Center), run **one folder and one pipeline job per SCM**. Each job uses a **wrapper** script that sets `SCM_PROVIDER` and `SCM_URL` for that SCM, then runs the same main pipeline. Global environment variables cannot define different values per job, so the wrapper is required for multi-SCM.

---

## 1. Wrapper pipeline (per-SCM script)

The repo provides a wrapper that sets SCM env vars and then runs the main pipeline:

- **`docker/jenkins/Jenkinsfile.multi-scm-wrapper`** – template: set `SCM_PROVIDER` and `SCM_URL` at the top, then `load 'docker/jenkins/mainPipeline.groovy'`.

**For each SCM you use**, do one of the following.

### Option A – One wrapper file per SCM (recommended)

1. Copy the wrapper into a named file in the same directory, e.g.:
   - `docker/jenkins/Jenkinsfile.wrapper-gitea`
   - `docker/jenkins/Jenkinsfile.wrapper-github`
   - `docker/jenkins/Jenkinsfile.wrapper-bitbucket`
2. Edit the **two lines** at the top of that copy:

   ```groovy
   env.SCM_PROVIDER = 'gitea'   // or 'github', 'gitlab', 'bitbucket'
   env.SCM_URL = 'https://gitea.example.com'   // your SCM API URL
   ```

   Examples:
   - Gitea: `env.SCM_PROVIDER = 'gitea'`, `env.SCM_URL = 'https://gitea.example.com'`
   - GitHub: `env.SCM_PROVIDER = 'github'`, `env.SCM_URL = 'https://api.github.com'`
   - GitLab: `env.SCM_PROVIDER = 'gitlab'`, `env.SCM_URL = 'https://gitlab.com'` (or your GitLab URL)
   - Bitbucket DC: `env.SCM_PROVIDER = 'bitbucket'`, `env.SCM_URL = 'https://bitbucket.example.com/rest/api/1.0'`
3. Commit the new file (e.g. `Jenkinsfile.wrapper-gitea`) to your repo or branch so the job can load it via “Pipeline script from SCM”.

### Option B – Use the template as-is and edit per branch

If you prefer not to add files, you can point the job at `docker/jenkins/Jenkinsfile.multi-scm-wrapper` and edit that single file in your repo to the correct `SCM_PROVIDER` / `SCM_URL` for the job. Then use a separate branch or repo copy per SCM, or change the file before each run (not ideal for webhooks). Option A is simpler for multiple fixed SCMs.

---

## 2. One folder and one job per SCM

| Step | What to do |
|------|------------|
| **Folder** | Create a folder per SCM (e.g. `code-review-gitea`, `code-review-github`). |
| **Credentials** | In the folder: **Credentials** → **Global** domain → **Add credentials**. Add **Secret text** with ID `SCM_TOKEN` (token for that SCM) and `GOOGLE_API_KEY`. |
| **Pipeline job** | Inside the folder, create a **Pipeline** job (e.g. `code-review`). **Pipeline script from SCM** → this repo, **Script Path**: the wrapper for this SCM (e.g. `docker/jenkins/Jenkinsfile.wrapper-gitea`). Do **not** use `docker/jenkins/Jenkinsfile` here—that script expects global env and is for single-SCM only. |
| **Webhook** | In each SCM, point the repo webhook to **this job’s** Generic Webhook Trigger URL. Use the JSONPath for that SCM (Gitea/GitHub/GitLab: [JENKINS-EXISTING](JENKINS-EXISTING.md#4-webhooks-so-prs-trigger-the-job); Bitbucket DC: [Bitbucket Data Center](BITBUCKET-DATACENTER.md)). |

No global `SCM_PROVIDER` / `SCM_URL` are needed for these jobs; the wrapper sets them per job.

---

## 3. Example: Gitea + GitHub

1. **Folder `code-review-gitea`**  
   - Credentials in folder: `SCM_TOKEN` = Gitea token, `GOOGLE_API_KEY`.  
   - Job `code-review`: **Script Path** = `docker/jenkins/Jenkinsfile.wrapper-gitea` (with `SCM_PROVIDER = 'gitea'`, `SCM_URL = 'https://gitea.example.com'` in that file).  
   - Webhook: Gitea repos → this job’s webhook URL.

2. **Folder `code-review-github`**  
   - Credentials in folder: `SCM_TOKEN` = GitHub token, `GOOGLE_API_KEY`.  
   - Job `code-review`: **Script Path** = `docker/jenkins/Jenkinsfile.wrapper-github` (with `SCM_PROVIDER = 'github'`, `SCM_URL = 'https://api.github.com'` in that file).  
   - Webhook: GitHub repos → this job’s webhook URL.

Each PR triggers only the pipeline for its SCM; credentials and SCM URL are isolated per folder/job.

---

## 4. Bitbucket Data Center

Same pattern: one folder, one job, wrapper file (e.g. `Jenkinsfile.wrapper-bitbucket`) with `env.SCM_PROVIDER = 'bitbucket'` and `env.SCM_URL = 'https://bitbucket.example.com/rest/api/1.0'`. Use the Bitbucket webhook JSONPath and filter from [Bitbucket Data Center](BITBUCKET-DATACENTER.md).

---

## 5. Summary

- **Single SCM**: use **Script Path** `docker/jenkins/Jenkinsfile` and set **global** env vars (`SCM_PROVIDER`, `SCM_URL`) in Jenkins. See [Jenkins (existing installation)](JENKINS-EXISTING.md).
- **Multiple SCMs**: do **not** rely on global env for SCM; use a **wrapper** per SCM. Copy `Jenkinsfile.multi-scm-wrapper` to e.g. `Jenkinsfile.wrapper-gitea`, edit the two env lines, set **Script Path** to that file, and use folder-scoped credentials and one webhook URL per job.
