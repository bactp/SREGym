# Runs the SREGym conductor/main.py process itself (not the agent under test,
# not the MCP server). Deployed as the "runner" container of a Job Pod alongside
# a docker:dind sidecar — see deploy/runner/k8s/job-template.yaml.
FROM python:3.12-slim

WORKDIR /opt/app

# curl (kubectl download + healthchecks), kubectl, helm (conductor shells out to
# both to deploy Prometheus/Jaeger/OpenEBS/ingress-nginx/MCP server into the
# target cluster), docker-ce-cli (talks to the dind sidecar over DOCKER_HOST;
# no daemon needed here), and uv for Python deps.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# All curl calls use -f (hard-fail on HTTP errors, e.g. GitHub/Docker Hub 429
# rate limits) --retry (transient-failure resilience) so a silently-truncated
# or empty download can't slip through `&& `/pipe chains undetected.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
    && ARCH=$(dpkg --print-architecture) \
    && curl -fL --retry 5 --retry-delay 5 -o kubectl \
        "https://dl.k8s.io/release/$(curl -fsSL --retry 5 --retry-delay 5 https://dl.k8s.io/release/stable.txt)/bin/linux/${ARCH}/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl \
    && HELM_VERSION=v3.16.3 \
    && curl -fL --retry 5 --retry-delay 5 -o helm.tar.gz \
        "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" \
    && tar -zxf helm.tar.gz \
    && install -o root -g root -m 0755 "linux-${ARCH}/helm" /usr/local/bin/helm \
    && rm -rf helm.tar.gz "linux-${ARCH}" \
    && helm version \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL --retry 5 --retry-delay 5 https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && curl -fsSL --retry 5 --retry-delay 5 https://astral.sh/uv/install.sh -o /tmp/install-uv.sh \
    && sh /tmp/install-uv.sh \
    && rm /tmp/install-uv.sh \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:$PATH"

# Project files. SREGym-applications/ (the app manifests submodule) and
# docker/agents/ (build.sh + Dockerfile + requirements-container.txt +
# install-scripts/, used by container_runner.py to auto-build the agent image)
# are baked in here so both the runner process AND the dind sidecar (which
# shares /workspace, not /opt/app, with this container — see job-template.yaml)
# can be seeded via the initContainer.
COPY pyproject.toml uv.lock ./
COPY main.py agents.yaml ./
COPY sregym/ ./sregym/
COPY clients/ ./clients/
COPY logger/ ./logger/
COPY llm_backend/ ./llm_backend/
COPY atif_converter/ ./atif_converter/
COPY docker/ ./docker/
COPY scripts/ ./scripts/
COPY SREGym-applications/ ./SREGym-applications/

RUN uv sync --frozen --no-dev

ENV PATH="/opt/app/.venv/bin:$PATH"
ENV PYTHONPATH=/opt/app

# main.py resolves `results/`, `.runtime`, `./logs`, and `./SREGym-applications`
# relative to CWD — the Job sets workingDir to a PVC-backed /workspace (see
# job-template.yaml), never /opt/app, so results survive Pod deletion and are
# visible to the dind sidecar for its bind-mount sources.
ENTRYPOINT ["/bin/sh", "-c"]
