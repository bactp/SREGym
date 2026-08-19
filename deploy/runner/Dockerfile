# syntax=docker/dockerfile:1
# Runs the SREGym conductor/main.py process itself (not the agent under test,
# not the MCP server). Deployed as the sole container of a Job Pod — see
# deploy/runner/k8s/job-base/job.yaml. The agent under test is a KAgent Agent
# CRD living on this same mgmt cluster, which is why one container suffices;
# the earlier Stratus design's docker:dind sidecar is gone (deploy/runner/
# README.md, "Why this is simpler than a Stratus-based runner").
FROM python:3.12-slim

WORKDIR /opt/app

# curl (downloads below + healthchecks), kubectl, helm (conductor shells out to
# both to deploy Prometheus/Jaeger/OpenEBS/ingress-nginx/MCP server into the
# target cluster), the kagent CLI (clients/kagent/driver.py shells out to
# `kagent invoke` -- see docs/kagent-integration.md#2.5), and uv for Python
# deps. The smaller tools are each needed twice over, so don't drop one on the
# assumption it only serves the installer:
#   tar     — unpacks the helm tarball in this very RUN, and get-kagent hard-
#             errors without it ("required to extract the kagent binary").
#   openssl — get-kagent verifies its download's checksum with `openssl sha1
#             -sha256` (skippable only via VERIFY_CHECKSUM=false), AND the
#             webhook/clock-drift fault generators shell out to `openssl
#             genrsa`/`req` at runtime to mint test CAs (e.g. sregym/conductor/
#             problems/admission_webhook_tls_mismatch.py:107).
#   jq      — get-kagent only; it parses the release JSON and version output.
# git is NOT needed: get-kagent warns "Could not find git" but that gates only
# its optional plugin installation, and nothing else here shells out to git.
#
# docker-ce-cli is installed for one reason only: conductor.py's unconditional
# `dependency_check(["kubectl", "helm", "docker"])` (sregym/conductor/
# conductor.py:461) aborts the run if the binary is absent. Nothing here talks
# to a Docker daemon — container_runner.py's `docker run` path is only taken by
# container_isolation: true agents (Stratus), and this image is deployed with
# KAgent agents, which never touch it.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# All curl calls use -f (hard-fail on HTTP errors, e.g. GitHub/Docker Hub 429
# rate limits) --retry (transient-failure resilience) so a silently-truncated
# or empty download can't slip through `&& `/pipe chains undetected.
#
# No gnupg: the docker-ce-cli repo is added with `signed-by=` pointing at the
# ASCII-armored key as downloaded, which apt verifies via its built-in gpgv.
# Nothing here shells out to gpg/apt-key, so the package would be dead weight.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        openssl \
        jq \
        tar \
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
    && curl -fsSL --retry 5 --retry-delay 5 https://raw.githubusercontent.com/kagent-dev/kagent/refs/heads/main/scripts/get-kagent \
        -o /tmp/get-kagent.sh \
    && bash /tmp/get-kagent.sh \
    && rm /tmp/get-kagent.sh \
    && kagent version \
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

# Project files. SREGym-applications/ (the app manifests submodule) is baked in
# because the Job's startup command copies it from here into the PVC-backed
# /workspace before launching main.py (see job-base/job.yaml) — nothing clones
# submodules at runtime, so git is deliberately not installed.
#
# docker/agents/ (build.sh + Dockerfile + requirements-container.txt +
# install-scripts/) is used by container_runner.py to auto-build the agent
# image. Unused on the KAgent path this image is deployed with, but kept so the
# same image still works for container_isolation: true agents given a daemon.
COPY pyproject.toml uv.lock ./

# geni-lib-xlab is a local-path dependency (pyproject.toml [tool.uv.sources]),
# so its tarball has to exist before uv resolves anything. Copy just that
# subtree rather than all of scripts/, to keep unrelated script edits from
# busting the dependency layer below.
COPY scripts/geni_lib/mod/ ./scripts/geni_lib/mod/

# Third-party deps only. Installed from just pyproject.toml/uv.lock, before any
# source/chart COPY below, and with a persistent BuildKit cache mount for uv's
# package cache — so editing application code (or the chart-vendoring steps
# further down) never forces the ~250-package reinstall to redo from a cold
# cache. This used to sit after all the COPY lines, so any source edit
# invalidated it.
#
# --no-install-project is what makes that split legal: the root `sregym`
# distribution is a real setuptools package ([tool.setuptools] packages), so
# installing it here — before its source trees are COPY'd — would fail.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY main.py agents.yaml ./
COPY sregym/ ./sregym/
COPY mcp_server/ ./mcp_server/
COPY clients/ ./clients/
COPY logger/ ./logger/
COPY llm_backend/ ./llm_backend/
COPY atif_converter/ ./atif_converter/
COPY docker/ ./docker/
COPY scripts/ ./scripts/
COPY SREGym-applications/ ./SREGym-applications/

# Now that the source trees exist, install the root `sregym` project itself.
# Cheap — the dependency closure above is already resolved and installed, so
# this only builds the one local distribution.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# opentelemetry-demo's Chart.yaml declares remote subchart dependencies
# (opentelemetry-collector, jaeger, prometheus, grafana, opensearch) but the
# repo doesn't vendor the fetched charts/ dir, so `helm install` fails with
# "chart dependencies" errors unless they're built in here at image build time.
# `helm repo add`/`dependency build` is avoided: it requires downloading each
# repo's full index.yaml first, and prometheus-community's is huge (~30MB) and
# routinely times out. Instead, pull the exact pinned versions (Chart.lock)
# directly from each project's GitHub Releases, which is fast regardless.
RUN mkdir -p SREGym-applications/astronomy-shop/charts/opentelemetry-demo/charts \
    && cd SREGym-applications/astronomy-shop/charts/opentelemetry-demo/charts \
    && curl -fL --retry 5 --retry-delay 5 -o opentelemetry-collector-0.142.2.tgz \
        "https://github.com/open-telemetry/opentelemetry-helm-charts/releases/download/opentelemetry-collector-0.142.2/opentelemetry-collector-0.142.2.tgz" \
    && curl -fL --retry 5 --retry-delay 5 -o jaeger-4.3.0.tgz \
        "https://github.com/jaegertracing/helm-charts/releases/download/jaeger-4.3.0/jaeger-4.3.0.tgz" \
    && curl -fL --retry 5 --retry-delay 5 -o prometheus-28.2.0.tgz \
        "https://github.com/prometheus-community/helm-charts/releases/download/prometheus-28.2.0/prometheus-28.2.0.tgz" \
    && curl -fL --retry 5 --retry-delay 5 -o grafana-10.5.8.tgz \
        "https://github.com/grafana/helm-charts/releases/download/grafana-10.5.8/grafana-10.5.8.tgz" \
    && curl -fL --retry 5 --retry-delay 5 -o opensearch-3.4.0.tgz \
        "https://github.com/opensearch-project/helm-charts/releases/download/opensearch-3.4.0/opensearch-3.4.0.tgz"

# sregym/service/telemetry/loki.py runs `helm repo add grafana
# https://grafana.github.io/helm-charts` on every problem run (Loki/Promtail
# are deployed for every problem, not just astronomy-shop ones) — same
# huge/slow index.yaml problem as above, but it's swallowed by a bare
# `except Exception` there, so it fails silently and Loki/Promtail never
# install. Bake a local file-based Helm repo with the pinned chart versions
# so that call never touches the network; loki.py points "grafana" at this
# path instead of the live URL (see helm-repos-comment there).
RUN mkdir -p helm-repos/grafana \
    && cd helm-repos/grafana \
    && curl -fL --retry 5 --retry-delay 5 -o loki-7.3.0.tgz \
        "https://github.com/grafana/helm-charts/releases/download/helm-loki-7.3.0/loki-7.3.0.tgz" \
    && curl -fL --retry 5 --retry-delay 5 -o promtail-6.16.6.tgz \
        "https://github.com/grafana/helm-charts/releases/download/promtail-6.16.6/promtail-6.16.6.tgz" \
    && helm repo index .

ENV PATH="/opt/app/.venv/bin:$PATH"
ENV PYTHONPATH=/opt/app

# main.py resolves `results/`, `.runtime`, `./logs`, and `./SREGym-applications`
# relative to CWD — the Job sets workingDir to a PVC-backed /workspace (see
# job-base/job.yaml, which mounts the shared PVC at a per-Job subPathExpr),
# never /opt/app, so results survive Pod deletion and stay collectable.
ENTRYPOINT ["/bin/sh", "-c"]
