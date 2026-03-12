# Using a new version of the review tool when code changes

After you change Python code (e.g. under `src/`) or dependencies in `pyproject.toml`, Jenkins will only use the new version if you update what it runs. The Jenkinsfile does not rebuild the image or reinstall the package for you; it runs whatever image or CLI is already on the node.

| How the job runs | What to do so Jenkins uses the new version |
|------------------|--------------------------------------------|
| **Container** (default) | On each Jenkins node that runs the job: **rebuild the image** and use that image. From the repo root: `docker build -t code-review-agent -f docker/Dockerfile.agent .` (or `podman build ...`). If you use a registry, build and push a new image, then pull and tag it on the nodes (or use a tag that your nodes pull on each run). If you use the [Quick Start](QUICKSTART.md) Docker Compose stack, run `docker compose up -d --build` so the agent image is rebuilt and the stack restarted. |
| **Inline** (`USE_INLINE_AGENT=true`) | On each node that runs the job: **reinstall or update** the package (e.g. pull the repo and run `pip install -e .` again, or install a new wheel). |

Re-run the build/pull or install step after code changes so the next pipeline run uses the updated tool.
