FROM jenkins/jenkins:2.552

# Install Docker CLI and Podman so Jenkins can run the agent container
# with either runtime (controlled by the CONTAINER_RUNTIME env var).
# uidmap provides newuidmap/newgidmap required for rootless Podman.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io podman uidmap python3 python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && usermod -aG docker jenkins

# Install code-review agent for inline mode (USE_INLINE_AGENT=true) when rootless Podman uid_map fails.
COPY pyproject.toml /tmp/agent-build/
COPY src /tmp/agent-build/src/
RUN pip3 install --break-system-packages --no-cache-dir /tmp/agent-build && rm -rf /tmp/agent-build

# Preinstall required plugins so the UI includes Credentials + Pipeline.
COPY docker/jenkins/plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

# Seed admin user and optional credentials on first boot.
COPY docker/jenkins/init.groovy.d/ /usr/share/jenkins/ref/init.groovy.d/

# Entrypoint fixes JENKINS_HOME ownership for rootless Podman, then runs Jenkins as jenkins user.
COPY docker/jenkins/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
USER jenkins
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
