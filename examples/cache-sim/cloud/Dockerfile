# The toolchain cachesim builds and runs with, plus python3 and git for cloudsweep's worker.
# For reproducible rebuilds, pin the base image by digest and the package versions.
FROM docker.io/library/debian:bookworm-slim
# The apt sandbox line lets this build under rootless podman without subordinate user IDs.
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99sandbox \
 && apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev make git python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
