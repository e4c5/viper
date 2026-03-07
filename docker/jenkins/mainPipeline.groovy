// Loadable pipeline: reads SCM_PROVIDER, SCM_URL (and other env) from the environment.
// Single-SCM: use Jenkinsfile (loads this); global env supplies SCM_PROVIDER/SCM_URL.
// Multi-SCM: use Jenkinsfile.multi-scm-wrapper (sets env per job, then loads this).

pipeline {
    agent any

    environment {
        GOOGLE_API_KEY = credentials('GOOGLE_API_KEY')
    }

    parameters {
        string(name: 'SCM_OWNER', description: 'Repo owner / project key')
        string(name: 'SCM_REPO', description: 'Repo name / slug')
        string(name: 'SCM_PR_NUM', description: 'PR number / id')
        string(name: 'SCM_HEAD_SHA', description: 'Head commit SHA')
        string(name: 'PR_ACTION', description: 'Webhook action or eventKey')
        string(name: 'LLM_PROVIDER', defaultValue: 'gemini', description: 'LLM provider')
        string(name: 'LLM_MODEL', defaultValue: 'gemini-2.5-flash', description: 'LLM model')
        string(name: 'COMPOSE_PROJECT_NAME', defaultValue: 'code-review', description: 'Docker Compose project name')
        string(name: 'IMAGE_NAME', defaultValue: 'code-review-agent', description: 'Container image name (or user/name); must match published image when using container agent')
    }

    stages {
        stage('Code Review') {
            steps {
                script {
                    def image = env.IMAGE_NAME?.trim() ?: params.IMAGE_NAME?.trim() ?: 'code-review-agent'
                    def projectName = env.COMPOSE_PROJECT_NAME ?: params.COMPOSE_PROJECT_NAME
                    def network = "${projectName}_code-review-net"
                    def scmOwner = params.SCM_OWNER ?: env.SCM_OWNER
                    def scmRepo = params.SCM_REPO ?: env.SCM_REPO
                    def scmPrNum = params.SCM_PR_NUM ?: env.SCM_PR_NUM
                    def scmHeadSha = params.SCM_HEAD_SHA ?: env.SCM_HEAD_SHA
                    def prAction = params.PR_ACTION ?: env.PR_ACTION
                    def llmProvider = params.LLM_PROVIDER ?: env.LLM_PROVIDER
                    def llmModel = params.LLM_MODEL ?: env.LLM_MODEL
                    def containerRuntime = env.CONTAINER_RUNTIME ?: 'docker'

                    def isBitbucket = (prAction != null && prAction.startsWith('pr:'))
                    def allowedActions = isBitbucket
                        ? ['pr:opened', 'pr:modified', 'pr:from_ref_updated']
                        : ['opened', 'synchronize']
                    def credId = 'SCM_TOKEN'
                    def scmProvider = isBitbucket ? 'bitbucket' : (env.SCM_PROVIDER ?: 'gitea')
                    def scmUrl = isBitbucket
                        ? (env.SCM_URL ?: env.SCM_URL_BITBUCKET ?: '')
                        : (env.SCM_URL ?: 'http://gitea:3000')
                    def unresolvedMarker = isBitbucket ? '$.pullRequest' : '$.pull_request'

                    if (prAction && !allowedActions.contains(prAction)) {
                        currentBuild.description = "Skipped PR action: ${prAction}"
                        echo "Skipping PR action '${prAction}'. Allowed: ${allowedActions.join(', ')}."
                        return
                    }

                    if (isBitbucket && !scmUrl?.trim()) {
                        error('Bitbucket job: set SCM_URL or SCM_URL_BITBUCKET to your Bitbucket API base (e.g. https://bitbucket.example.com/rest/api/1.0). See docs/BITBUCKET-DATACENTER.md.')
                    }

                    if (!scmOwner || scmOwner.contains(unresolvedMarker)) {
                        error("SCM_OWNER not resolved. Check Generic Webhook Trigger JSONPath (${isBitbucket ? 'Bitbucket' : 'Gitea'} doc).")
                    }
                    if (!scmRepo || scmRepo.contains(unresolvedMarker)) {
                        error('SCM_REPO not resolved. Check Generic Webhook Trigger JSONPath.')
                    }
                    if (!scmPrNum || scmPrNum.contains(unresolvedMarker)) {
                        error('SCM_PR_NUM not resolved. Check Generic Webhook Trigger JSONPath.')
                    }
                    if (!scmHeadSha || scmHeadSha.contains(unresolvedMarker)) {
                        error('SCM_HEAD_SHA not resolved. Check Generic Webhook Trigger JSONPath.')
                    }

                    def useInlineAgent = env.USE_INLINE_AGENT == 'true' || env.USE_INLINE_AGENT == '1'
                    withCredentials([string(credentialsId: credId, variable: 'SCM_TOKEN')]) {
                        withEnv([
                            "CONTAINER_RUNTIME=${containerRuntime}",
                            "REVIEW_IMAGE=${image}",
                            "REVIEW_NETWORK=${network}",
                            "REVIEW_SCM_OWNER=${scmOwner}",
                            "REVIEW_SCM_REPO=${scmRepo}",
                            "REVIEW_SCM_PR_NUM=${scmPrNum}",
                            "REVIEW_SCM_HEAD_SHA=${scmHeadSha}",
                            "REVIEW_SCM_PROVIDER=${scmProvider}",
                            "REVIEW_SCM_URL=${scmUrl}",
                            "REVIEW_LLM_PROVIDER=${llmProvider}",
                            "REVIEW_LLM_MODEL=${llmModel}",
                            "USE_INLINE_AGENT=${useInlineAgent}",
                        ]) {
                            sh '''
                                set -euo pipefail
                                if [ "$USE_INLINE_AGENT" = "true" ] || [ "$USE_INLINE_AGENT" = "1" ]; then
                                    export SCM_PROVIDER="$REVIEW_SCM_PROVIDER" SCM_URL="$REVIEW_SCM_URL" SCM_TOKEN="$SCM_TOKEN"
                                    export LLM_PROVIDER="$REVIEW_LLM_PROVIDER" LLM_MODEL="$REVIEW_LLM_MODEL"
                                    code-review --owner "$REVIEW_SCM_OWNER" --repo "$REVIEW_SCM_REPO" --pr "$REVIEW_SCM_PR_NUM" --head-sha "$REVIEW_SCM_HEAD_SHA" --print-findings
                                else
                                    runtime="$CONTAINER_RUNTIME"
                                    if [ -z "$runtime" ] || ! command -v "$runtime" >/dev/null 2>&1; then
                                        if command -v docker >/dev/null 2>&1; then runtime=docker
                                        elif command -v podman >/dev/null 2>&1; then runtime=podman
                                        else echo "ERROR: No container runtime. Set CONTAINER_RUNTIME or USE_INLINE_AGENT=true (docs/QUICKSTART.md)."; exit 1; fi
                                    fi
                                    "$runtime" run --rm --network "$REVIEW_NETWORK" \
                                        -e SCM_PROVIDER="$REVIEW_SCM_PROVIDER" \
                                        -e SCM_URL="$REVIEW_SCM_URL" \
                                        -e SCM_TOKEN="$SCM_TOKEN" \
                                        -e SCM_OWNER="$REVIEW_SCM_OWNER" \
                                        -e SCM_REPO="$REVIEW_SCM_REPO" \
                                        -e SCM_PR_NUM="$REVIEW_SCM_PR_NUM" \
                                        -e SCM_HEAD_SHA="$REVIEW_SCM_HEAD_SHA" \
                                        -e LLM_PROVIDER="$REVIEW_LLM_PROVIDER" \
                                        -e LLM_MODEL="$REVIEW_LLM_MODEL" \
                                        -e PYTHONUNBUFFERED=1 \
                                        -e GOOGLE_API_KEY \
                                        "$REVIEW_IMAGE" --owner "$REVIEW_SCM_OWNER" --repo "$REVIEW_SCM_REPO" --pr "$REVIEW_SCM_PR_NUM" --head-sha "$REVIEW_SCM_HEAD_SHA" --print-findings \
                                    || { echo "HINT: Podman issues? Set USE_INLINE_AGENT=true (docs/QUICKSTART.md)."; exit 1; }
                                fi
                            '''
                        }
                    }
                }
            }
        }
    }
}
