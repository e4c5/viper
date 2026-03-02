FROM jenkins/jenkins:2.552

# Install Docker CLI and Podman so Jenkins can run the agent container
# with either runtime (controlled by the CONTAINER_RUNTIME env var).
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io podman \
    && rm -rf /var/lib/apt/lists/* \
    && usermod -aG docker jenkins
USER jenkins

# Preinstall required plugins so the UI includes Credentials + Pipeline.
COPY docker/jenkins/plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

# Seed admin user and optional credentials on first boot.
COPY docker/jenkins/init.groovy.d/ /usr/share/jenkins/ref/init.groovy.d/
