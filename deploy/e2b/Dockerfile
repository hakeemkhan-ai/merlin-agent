FROM python:3.13-bookworm
USER root
ENV DEBIAN_FRONTEND=noninteractive
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/merlin-browsers
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git ripgrep procps tini && rm -rf /var/lib/apt/lists/*
# E2B's Dockerfile parser cannot copy from another image/build stage.
# Install the official Node archive and verify it against the release checksums.
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
        amd64) merlin_node_arch=x64 ;; \
        arm64) merlin_node_arch=arm64 ;; \
        *) exit 1 ;; \
    esac; \
    merlin_node_archive="node-v24.20.0-linux-${merlin_node_arch}.tar.gz"; \
    mkdir -p /tmp/merlin-node; \
    cd /tmp/merlin-node; \
    curl -fsSL --retry 3 -O "https://nodejs.org/dist/v24.20.0/${merlin_node_archive}"; \
    curl -fsSL --retry 3 -O https://nodejs.org/dist/v24.20.0/SHASUMS256.txt; \
    sha256sum --check --ignore-missing SHASUMS256.txt; \
    tar -xzf "$merlin_node_archive" -C /usr/local --strip-components=1 --no-same-owner; \
    cd /; \
    rm -rf /tmp/merlin-node
RUN python3 -m venv /opt/merlin-tools && \
    /opt/merlin-tools/bin/pip install --no-cache-dir playwright==1.64.1 pyright==1.1.408 && \
    /opt/merlin-tools/bin/playwright install --with-deps chromium && \
    chmod -R a+rX /opt/merlin-browsers && \
    npm install --global typescript@5.9.3 typescript-language-server@5.1.3
ENV PATH="/opt/merlin-tools/bin:${PATH}"
RUN (id -u user >/dev/null 2>&1 || useradd --create-home --shell /bin/bash user) && \
    mkdir -p /home/user/repo && chown -R user:user /home/user
USER user
WORKDIR /home/user/repo
