# Running the code-review agent with multiple SCMs

If your team uses **more than one SCM** (e.g. Gitea and GitHub, or GitHub and Bitbucket Data Center), run **one folder and one pipeline job per SCM**. Each job uses the **same** Jenkinsfile (`docker/jenkins/Jenkinsfile`); you set **`SCM_PROVIDER` and `SCM_URL` as folder-level environment variables** so each job talks to the right SCM. No wrapper file or repo copies needed.

---

## How it works

The Jenkinsfile reads **`SCM_PROVIDER`** and **`SCM_URL`** from the environment (and optionally from parameters). So:

- **Single SCM**: One job, **Script Path** `docker/jenkins/Jenkinsfile`. Set `SCM_PROVIDER` and `SCM_URL` in **Manage Jenkins → Global properties → Environment variables**. See [Jenkins (existing installation)](JENKINS-EXISTING.md).
- **Multiple SCMs**: One job **per** SCM, each with **Script Path** `docker/jenkins/Jenkinsfile`. Set **`SCM_PROVIDER` and `SCM_URL` on the folder** that contains the job (folder → Configure → Environment variables or Properties). Do **not** rely on job parameter defaults—when the pipeline script is loaded from SCM, Jenkins can overwrite parameter default values with the ones in the Jenkinsfile, so they reset. Folder environment variables persist and are used by the pipeline.

---

## 1. One folder and one job per SCM

| Step | What to do |
|------|------------|
| **Folder** | Create a folder per SCM (e.g. `code-review-gitea`, `code-review-github`). |
| **Folder env vars** | Open the folder → **Configure**. If you see an editable **Environment variables** or **Folder Properties** section, add **`SCM_PROVIDER`** and **`SCM_URL`** there (e.g. `gitea` and `https://gitea.example.com`). If **Properties** is not editable, install the **Folder Properties** or **Environment Injector** plugin; see [If “Properties” is not editable](#if-properties-is-not-editable-use-a-plugin) below. |
| **Credentials** | In the folder: **Credentials** → click **Global** domain → **Add credentials**. Add **Secret text** with ID `SCM_TOKEN` (token for that SCM) and `LLM_API_KEY`. |
| **Pipeline job** | Inside the folder, create a **Pipeline** job (e.g. `code-review`). **Pipeline script from SCM** → this repo, **Script Path**: `docker/jenkins/Jenkinsfile`. No need to set SCM parameter defaults on the job—the folder env vars are used. |
| **Webhook** | In each SCM, point the repo webhook to **this job’s** Generic Webhook Trigger URL. Use the JSONPath for that SCM (Gitea/GitHub/GitLab: [JENKINS-EXISTING](JENKINS-EXISTING.md#4-webhooks-so-prs-trigger-the-job); Bitbucket DC: [Bitbucket Data Center](BITBUCKET-DATACENTER.md)). |

No global `SCM_PROVIDER` / `SCM_URL` are needed; each folder’s environment variables supply them for that job.

### If “Properties” is not editable: use a plugin

The built-in **Folders** plugin often does not let you edit folder properties. Install one of these so you can set `SCM_PROVIDER` and `SCM_URL` without using job parameter defaults:

| Plugin | Use case | How to set SCM_PROVIDER / SCM_URL |
|--------|----------|-----------------------------------|
| **[Environment Injector](https://plugins.jenkins.io/envinject/)** (recommended) | Per-job; no Jenkinsfile change | **Manage Jenkins → Plugins** → install **Environment Injector**. In each job → **Configure** → **Build Environment** → enable **Inject environment variables** → in **Properties Content** add one line per variable, e.g. `SCM_PROVIDER=gitea` and `SCM_URL=https://gitea.example.com`. Save. The pipeline reads these from `env`. |
| **[Folder Properties](https://plugins.jenkins.io/folder-properties/)** | One place per folder | **Manage Jenkins → Plugins** → install **Folder Properties**. Open the folder → **Configure** → **Folder Properties** (editable) → **Add property** → add `SCM_PROVIDER` and `SCM_URL`. To use them in the pipeline you must add `options { withFolderProperties() }` to the Jenkinsfile (e.g. keep a local copy of the Jenkinsfile with that line in the `pipeline { }` block). |

---

## 2. Example: Gitea + GitHub

1. **Folder `code-review-gitea`**  
   - **Configure** the folder: add **Environment variables** (or Properties → Environment variables) with `SCM_PROVIDER` = `gitea`, `SCM_URL` = `https://gitea.example.com`. Save.  
   - Credentials in folder: `SCM_TOKEN` = Gitea token, `LLM_API_KEY`.  
   - Job `code-review`: **Pipeline script from SCM** → this repo, **Script Path** = `docker/jenkins/Jenkinsfile`.  
   - Webhook: Gitea repos → this job’s webhook URL.

2. **Folder `code-review-github`**  
   - **Configure** the folder: add **Environment variables** with `SCM_PROVIDER` = `github`, `SCM_URL` = `https://api.github.com`. Save.  
   - Credentials in folder: `SCM_TOKEN` = GitHub token, `LLM_API_KEY`.  
   - Job `code-review`: **Pipeline script from SCM** → this repo, **Script Path** = `docker/jenkins/Jenkinsfile`.  
   - Webhook: GitHub repos → this job’s webhook URL.

Each PR triggers only the pipeline for its SCM; credentials and SCM URL are isolated per folder.

---

## 3. Bitbucket Data Center

Same pattern: one folder, one job, **Script Path** `docker/jenkins/Jenkinsfile`. Set **folder** environment variables `SCM_PROVIDER` = `bitbucket`, `SCM_URL` = `https://bitbucket.example.com/rest/api/1.0`. Use the Bitbucket webhook JSONPath and filter from [Bitbucket Data Center](BITBUCKET-DATACENTER.md).

---

## 4. Summary

- **Single SCM**: One job, **Script Path** `docker/jenkins/Jenkinsfile`, **global** env vars `SCM_PROVIDER` and `SCM_URL`. See [Jenkins (existing installation)](JENKINS-EXISTING.md).
- **Multiple SCMs**: One job per SCM, same **Script Path** `docker/jenkins/Jenkinsfile`. Set **folder** environment variables `SCM_PROVIDER` and `SCM_URL` in each folder’s **Configure** (not job parameter defaults, which get reset when the script is loaded from SCM). Use folder-scoped credentials and one webhook URL per job.
