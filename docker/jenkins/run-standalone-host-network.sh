#!/usr/bin/env bash
# Run the Jenkins image from docker-compose with host networking so the container
# (and the code-review agent container) can reach services on the host (e.g. Bitbucket).
#
# Usage: from repo root:  ./docker/jenkins/run-standalone-host-network.sh
# Stop:  podman stop code-review-jenkins   (or docker stop code-review-jenkins)
# No exports needed: script auto-detects Docker vs Podman and the socket path.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

IMAGE_NAME="${IMAGE_NAME:-code-review-jenkins}"
CONTAINER_NAME="${CONTAINER_NAME:-code-review-jenkins}"
JENKINS_HOME_VOLUME="${JENKINS_HOME_VOLUME:-code-review_jenkins_home}"
AGENT_NETWORK="${AGENT_NETWORK:-code-review_code-review-net}"

# ---- Auto-detect container runtime and socket ----
# Prefer podman if available; else docker. Set CONTAINER_RUNTIME and CONTAINER_SOCKET to override.
if [[ -n "${CONTAINER_RUNTIME:-}" && -n "${CONTAINER_SOCKET:-}" && -e "${CONTAINER_SOCKET}" ]]; then
  : # use env as-is
else
  CONTAINER_RUNTIME=""
  CONTAINER_SOCKET=""
fi

if [[ ( -z "$CONTAINER_RUNTIME" || -z "$CONTAINER_SOCKET" ) && command -v podman &>/dev/null && podman info &>/dev/null ]]; then
  CONTAINER_RUNTIME=podman
  CONTAINER_SOCKET="$(podman info --format '{{.Host.RemoteSocket.Path}}' 2>/dev/null)" || true
  if [[ -z "$CONTAINER_SOCKET" || ! -e "$CONTAINER_SOCKET" ]]; then
    [[ -S /var/run/podman/podman.sock ]] && CONTAINER_SOCKET=/var/run/podman/podman.sock
    [[ -z "$CONTAINER_SOCKET" && -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock" ]] && CONTAINER_SOCKET="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
  fi
fi

if [[ ( -z "$CONTAINER_RUNTIME" || -z "$CONTAINER_SOCKET" ) && -e /var/run/docker.sock && command -v docker &>/dev/null && docker info &>/dev/null ]]; then
  CONTAINER_RUNTIME=docker
  CONTAINER_SOCKET=/var/run/docker.sock
fi

# If "docker" is actually podman (e.g. docker symlink), use podman socket
if [[ "$CONTAINER_RUNTIME" = "docker" ]] && docker info 2>&1 | grep -qi podman; then
  CONTAINER_RUNTIME=podman
  CONTAINER_SOCKET="$(podman info --format '{{.Host.RemoteSocket.Path}}' 2>/dev/null)" || true
  [[ -z "$CONTAINER_SOCKET" && -S /var/run/podman/podman.sock ]] && CONTAINER_SOCKET=/var/run/podman/podman.sock
  [[ -z "$CONTAINER_SOCKET" && -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock" ]] && CONTAINER_SOCKET="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
fi

if [[ -z "$CONTAINER_RUNTIME" || -z "$CONTAINER_SOCKET" || ! -e "$CONTAINER_SOCKET" ]]; then
  echo "ERROR: Could not detect a container runtime and socket." >&2
  echo "  Install Docker or Podman and ensure the daemon is running, then run this script again." >&2
  exit 1
fi

echo "Using $CONTAINER_RUNTIME with socket $CONTAINER_SOCKET"

# ---- Build, volume, network ----
if ! "$CONTAINER_RUNTIME" image inspect "$IMAGE_NAME" &>/dev/null; then
  echo "Building $IMAGE_NAME..."
  "$CONTAINER_RUNTIME" build -t "$IMAGE_NAME" -f docker/jenkins/Dockerfile .
fi

"$CONTAINER_RUNTIME" volume inspect "$JENKINS_HOME_VOLUME" &>/dev/null || "$CONTAINER_RUNTIME" volume create "$JENKINS_HOME_VOLUME"
"$CONTAINER_RUNTIME" network inspect "$AGENT_NETWORK" &>/dev/null || "$CONTAINER_RUNTIME" network create "$AGENT_NETWORK"

# ---- Start Jenkins ----
echo "Starting Jenkins (host network). UI: http://localhost:8080"
echo "In Jenkins set SCM_URL to the host IP for Bitbucket (e.g. http://192.168.1.27:7990/rest/api/1.0); Bitbucket must listen on 0.0.0.0."
echo ""

"$CONTAINER_RUNTIME" run --rm -d \
  --name "$CONTAINER_NAME" \
  --network host \
  -v "$JENKINS_HOME_VOLUME:/var/jenkins_home" \
  -v "$CONTAINER_SOCKET:/var/run/docker.sock" \
  -e JAVA_OPTS="-Djenkins.install.runSetupWizard=false -Dhudson.model.ParametersAction.safeParameters=jenkins-generic-webhook-trigger-plugin_uuid" \
  -e JENKINS_ADMIN_USER=admin \
  -e JENKINS_ADMIN_PASS=admin \
  -e CONTAINER_RUNTIME="$CONTAINER_RUNTIME" \
  -e USE_INLINE_AGENT="${USE_INLINE_AGENT:-false}" \
  "$IMAGE_NAME"

echo "Container started. Logs: $CONTAINER_RUNTIME logs -f $CONTAINER_NAME"
