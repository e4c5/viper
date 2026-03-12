# Quick Start – Podman (rootless)

This guide explains how to run the **code review agent** stack with **Podman** (rootless) instead of Docker.

Use this if you prefer Podman or do not have Docker available on the host.

For Gitea/Jenkins/webhook setup, follow sections **2–5** of `docs/QUICKSTART.md`; only the container runtime parts differ.

---

## 1. Prerequisites

- **Podman** and **Podman Compose** (`podman-compose`)
- A user that will run `podman` and `podman-compose` (rootless)
- **LLM API key** (set as `LLM_API_KEY`; provider chosen via `LLM_PROVIDER`)

All commands below are run as **your normal user**, not root, unless explicitly prefixed with `sudo`.

---

## 2. Configure rootless Podman

Rootless Podman uses **user namespaces**. Your user needs subordinate UID/GID ranges and Podman must be migrated to use them.

From the host:

```bash
# Check current subuid/subgid allocation for your user
cat /etc/subuid | grep $(whoami) || echo "no subuid entry"
cat /etc/subgid | grep $(whoami) || echo "no subgid entry"
```

If you don’t see entries for your user, add them and migrate:

```bash
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $(whoami)
podman system migrate
```

> If your environment forbids user namespaces, you may still see `uid_map` / “Operation not permitted” errors. In that case, use **inline mode** (section 4) instead of container nesting.

---

## 3. Start the stack with Podman

From the **repository root** (the folder that contains `docker-compose.yml`):

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user start podman.socket

export CONTAINER_SOCKET=$XDG_RUNTIME_DIR/podman/podman.sock
export CONTAINER_RUNTIME=podman

ls -l "$CONTAINER_SOCKET"

podman-compose up -d --build
```

This:

- Starts a **rootless Podman socket** for your user.
- Mounts that socket into the Jenkins container (see `docker-compose.yml`).
- Runs Gitea + Jenkins on the `code-review-net` network.

You can now access:

- **Gitea**: http://localhost:3000  
- **Jenkins**: http://localhost:8080  

Follow sections **2–5** of `docs/QUICKSTART.md` to:

- Configure Gitea
- Configure Jenkins credentials
- Configure the Jenkins Pipeline job and webhook

---

## 4. Inline mode (no nested Podman)

If your environment does not allow rootless user namespaces (or you keep seeing `uid_map` / “Operation not permitted” when the pipeline runs), you can run the agent **inside the Jenkins container** instead of as a separate Podman container.

This avoids container nesting and does not require user namespaces.

### 4.1 Enable inline mode

From the repo root:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user start podman.socket

export CONTAINER_SOCKET=$XDG_RUNTIME_DIR/podman/podman.sock
export CONTAINER_RUNTIME=podman
export USE_INLINE_AGENT=true

ls -l "$CONTAINER_SOCKET"

podman-compose down
podman-compose up -d --build
```

In this mode:

- The Jenkins image already has the `code-review` CLI installed.
- The pipeline **does not call `podman run`**. Instead, it executes:

  ```bash
  code-review --owner ... --repo ... --pr ... --head-sha ...
  ```

  directly inside the Jenkins container.

This completely sidesteps Podman’s `uid_map` / `newuidmap` limitations.

---

## 5. Podman troubleshooting

Common issues when using Podman with this stack:

| Symptom | What to do |
|--------|-------------|
| `container name "code-review-..." is already in use` | Podman does not replace existing containers. Run `podman-compose down`, then `podman-compose up -d --build`. |
| `missing rw permissions on JENKINS_HOME` / `Permission denied` on `/var/jenkins_home` | The Jenkins image entrypoint fixes this on start. If it persists, remove the volume and recreate: `podman-compose down -v`, then `podman-compose up -d --build` (this deletes existing Jenkins and Gitea data). |
| `docker: not found` | Set `CONTAINER_RUNTIME=podman` and ensure the Jenkins image has Podman (the provided Jenkins image does). The pipeline also auto-detects Podman when Docker is missing. |
| `newuidmap: executable file not found` | Rebuild the Jenkins image so it includes the `uidmap` package: `podman-compose up -d --build`. The provided Jenkins Dockerfile already installs `uidmap`. |
| `newuidmap: write to uid_map failed: Operation not permitted` | Your host user needs subuid/subgid ranges. Run: `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $(whoami)` then `podman system migrate`. If this still fails (restricted environment), switch to **inline mode** (section 4). |

For any other issues, check the Jenkins Pipeline log; hints are printed when Podman-related failures occur (for example, suggesting inline mode on `uid_map` errors).

