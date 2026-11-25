# syntax=docker/dockerfile:1
# E2E-001 container image: hyprial Python daemon for the two-container roundtrip.
#
# Built once by run-two-machine-roundtrip.py and reused across runs, so
# `docker stop`/`docker start` restarts (offline-queue and self-heal phases)
# never re-install anything.  The lockfile-first layer keeps dependency
# downloads cached across source edits; only the project layer rebuilds.
FROM python:3.12-slim

LABEL hyprial-e2e001=1

ENV UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN --mount=type=secret,id=hyprial_e2e_ca \
    set -eu; \
    if [ -f /run/secrets/hyprial_e2e_ca ]; then \
      set -- -o Acquire::https::CaInfo=/run/secrets/hyprial_e2e_ca; \
    else set --; fi; \
    apt-get "$@" update \
    && apt-get "$@" install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# VERSION is where pyproject's dynamic version lives since d9382ea6; without
# it hatchling cannot build the project ("file does not exist: VERSION").
COPY pyproject.toml uv.lock VERSION ./
RUN --mount=type=secret,id=hyprial_e2e_ca \
    if [ -f /run/secrets/hyprial_e2e_ca ]; then \
      export PIP_CERT=/run/secrets/hyprial_e2e_ca SSL_CERT_FILE=/run/secrets/hyprial_e2e_ca; \
    fi; \
    pip install --no-cache-dir uv \
    && uv sync --locked --no-install-project

COPY src ./src
RUN --mount=type=secret,id=hyprial_e2e_ca \
    if [ -f /run/secrets/hyprial_e2e_ca ]; then \
      export SSL_CERT_FILE=/run/secrets/hyprial_e2e_ca; \
    fi; \
    uv sync --locked

COPY sidecar/hyprial-tailcat /tmp/hyprial-tailcat
RUN --mount=from=golang:1.27.1,source=/usr/local/go,target=/usr/local/go \
    --mount=type=cache,id=hyprial-e2e001-gomod,target=/var/cache/hyprial-e2e001/gomod \
    --mount=type=cache,id=hyprial-e2e001-gobuild,target=/var/cache/hyprial-e2e001/gobuild \
    --mount=type=secret,id=hyprial_e2e_ca \
    set -eu; \
    if [ -f /run/secrets/hyprial_e2e_ca ]; then \
      export SSL_CERT_FILE=/run/secrets/hyprial_e2e_ca GIT_SSL_CAINFO=/run/secrets/hyprial_e2e_ca; \
    fi; \
    export PATH="/usr/local/go/bin:$PATH" CGO_ENABLED=0 GOTOOLCHAIN=local \
      GOMODCACHE=/var/cache/hyprial-e2e001/gomod GOCACHE=/var/cache/hyprial-e2e001/gobuild; \
    cd /tmp/hyprial-tailcat; \
    mkdir -p /opt/hyprial/bin; \
    go build -p=2 -mod=readonly -trimpath -ldflags='-s -w' -o /opt/hyprial/bin/ \
      ./cmd/hyprial-tailcat ./cmd/hyprial-tailcat-labrelay
ENV HYPRIAL_TAILCAT_BINARY=/opt/hyprial/bin/hyprial-tailcat
RUN python -c 'from pathlib import Path; from hyprial.daemon import verify_tailcat_sidecar; verify_tailcat_sidecar(Path("/tmp/hyprial-e2e001-build-verify"))'

COPY contract/e2e/docker/machine_identity.py /app/e2e_machine_identity.py
COPY contract/e2e/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
RUN rm -f /etc/machine-id

ENTRYPOINT ["/entrypoint.sh"]
