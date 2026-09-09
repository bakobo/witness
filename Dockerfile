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
# The base is pinned by DIGEST, not by tag (OPS-F3). `python:3.14-slim-bookworm` is a moving
# target: two builds of the same commit weeks apart would carry different interpreters and
# different system libraries, which is the drift @lnk24kwp closes on one side and would leave
# open on the other. The digest is the multi-arch index, so multi-platform builds still work.
# To move it deliberately:
#     docker buildx imagetools inspect python:3.14-slim-bookworm --format '{{.Manifest.Digest}}'
#
# Exactly one native library is required: libsodium23. pysodium is a ctypes binding that
# dlopen()s libsodium.so.23 at import time, so without it `import keri` fails outright. That is
# the entire native surface.

# ---------------------------------------------------------------------------------------------
# Builder: resolves the dependency closure into a self-contained venv. All build tooling lives
# here and none of it reaches the runtime image.
# ---------------------------------------------------------------------------------------------
FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f AS builder

# git is build tooling: keripy is pinned as a git reference, so resolution needs a git client.
# Confined to this stage, which is the whole point of the split.
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

# The closure is entirely public (@qojsxe7s), so this resolves anonymously and the build takes no
# secret at all. It used to mount a BuildKit secret carrying a GitHub App token and rewrite an SSH
# URL to HTTPS, because heti was private; @s6v3qm replaced heti with fiki, which is public, and
# the machinery went with it rather than being left inert against a future that may not come.
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------------------------
# Runtime: the interpreter, one native library, and the resolved venv. No compiler, no git, no uv.
# ---------------------------------------------------------------------------------------------
FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f AS runtime

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
# /backup exists and is owned for the same reason: Docker copies a pre-created directory's
# ownership onto a fresh NAMED volume mounted over it, so `-v backups:/backup` just works. A bind
# mount from the host does not inherit that, so a host directory must be chowned to 1001 itself.
RUN useradd --uid 1001 --user-group --create-home --shell /usr/sbin/nologin witness \
    && mkdir -p /usr/local/var/keri /backup \
    && chown -R 1001:1001 /usr/local/var/keri /backup

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
# An orchestrator otherwise cannot tell "started" from "working", and for this image those differ
# in the way that matters: a wedged witness has a running process, an open port and an openable
# database. `degraded` is treated as unhealthy deliberately — that IS the wedge. Uses the
# interpreter already in the image rather than adding curl to a runtime layer that has one job.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD \
    python -c "import json,sys,urllib.request; \
r=urllib.request.urlopen('http://127.0.0.1:5633/v1/witness/health',timeout=4); \
sys.exit(0 if json.load(r).get('status')=='ok' else 1)"

ENTRYPOINT ["witness", "supervise"]
CMD ["--essential", "witness run --name witness --alias witness --http 5631 --tcp 5632", \
     "--auxiliary", "witness control-plane --name witness --host 0.0.0.0 --port 5633"]
