# syntax=docker/dockerfile:1.7
#
# The witness image: one image, one container, two processes (@a24p3kbw).
#
# Base and native dependencies are @vqdcca23. The short version: Debian rather than Alpine,
# because musl has no manylinux wheels and Alpine therefore turns every C extension in keripy's
# closure into a source build. On Debian every one of them ships a cp314 or abi3 wheel, so the
# runtime layer needs no compiler at all. If you find yourself adding gcc here, you have taken a
# wrong turn.
#
# Exactly one native library is required: libsodium23. pysodium is a ctypes binding that
# dlopen()s libsodium.so.23 at import time, so without it `import keri` fails outright. That is
# the entire native surface.

# ---------------------------------------------------------------------------------------------
# Builder: resolves the dependency closure into a self-contained venv. All build tooling lives
# here and none of it reaches the runtime image.
# ---------------------------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

# git is build tooling: both keripy and heti are pinned as git references, so resolution needs a
# git client. Confined to this stage, which is the whole point of the split.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.9.7

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# heti is private and pinned by SSH URL, because every Bakobo developer has SSH keys and a fresh
# clone should need no credential setup. A build has no SSH agent, so it rewrites that URL to
# HTTPS carrying a short-lived token, exactly as CI does for `uv sync` (.github/workflows/ci.yml).
# The token arrives as a BuildKit secret so it never lands in a layer or in `docker history`.
# Locally: --secret id=gh_token,env=GH_TOKEN with GH_TOKEN=$(gh auth token). No PAT is involved
# at either end. required=false so a future all-public closure still builds with no secret.
RUN --mount=type=secret,id=gh_token,required=false \
    set -eu; \
    if [ -s /run/secrets/gh_token ]; then \
        git config --global \
            url."https://x-access-token:$(cat /run/secrets/gh_token)@github.com/".insteadOf \
            "ssh://git@github.com/"; \
    fi; \
    uv sync --frozen --no-dev --no-editable
# No cleanup of that git config is needed: it lives in this stage's /root/.gitconfig, and only
# /opt/venv crosses into the runtime image.

# ---------------------------------------------------------------------------------------------
# Runtime: the interpreter, one native library, and the resolved venv. No compiler, no git, no uv.
# ---------------------------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="bakobo-witness" \
      org.opencontainers.image.description="A stock keripy witness and Bakobo's read-only control plane, co-located." \
      org.opencontainers.image.source="https://github.com/bakobo/witness" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends libsodium23 \
    && rm -rf /var/lib/apt/lists/*

# Non-root, and the keystore directory exists and is owned before the witness ever runs. keripy's
# Filer creates its tree with mode 0o1700 owned by the running uid, so both processes must run as
# the SAME uid: the control plane could not otherwise even read data.mdb, let alone register in
# lock.mdb, which @a24p3kbw requires it to do.
RUN useradd --uid 1001 --user-group --create-home --shell /usr/sbin/nologin witness \
    && mkdir -p /usr/local/var/keri \
    && chown -R 1001:1001 /usr/local/var/keri

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

USER 1001

# 5631 witness HTTP, 5632 witness CESR-over-TCP, 5633 control plane. These are `kli witness
# start`'s own defaults for the first two (note its runWitness() signature has them the other way
# round — a trap for anyone calling it directly). EXPOSE documents; it publishes nothing. Publish
# to loopback only (-p 127.0.0.1:5631:5631) so the reverse proxy stays the sole public listener,
# per infra @m4c35y. Note that Docker's published-port rules bypass the INPUT chain, so any
# host firewall or rate-limiting rules must be written in DOCKER-USER or they silently do nothing.
EXPOSE 5631 5632 5633

# The supervisor is PID 1: it forwards SIGTERM to both children and enforces the asymmetric
# failure policy (@c7v3kp — an auxiliary crash must never take the witness down).
#
# The runner is `witness run`, the launcher (@n5r2vq): keripy's own setupWitness doers unchanged,
# in a Doist that also carries the telemetry doer (@vxt7feoi). Because the supervisor takes
# commands as opaque strings, this line is the only thing that changed when it moved off
# `kli witness start` — no code, no rebuild of the supervisor.
#
# The control plane binds 0.0.0.0 here rather than its 127.0.0.1 default, and that is not a
# loosening: inside a container, loopback means "reachable only from this container", which no
# operator can use. The confinement moves out to the host, where `-p 127.0.0.1:5633:5633` gives
# the same property @h5n2rk wanted. Publishing it on 0.0.0.0 at the host is the mistake to avoid.
ENTRYPOINT ["witness", "supervise"]
CMD ["--essential", "witness run --name witness --alias witness --http 5631 --tcp 5632", \
     "--auxiliary", "witness control-plane --name witness --host 0.0.0.0 --port 5633"]
