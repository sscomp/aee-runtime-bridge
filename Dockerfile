# AEE Epic 9.5 — Docker Profiles (Master Plan §21.5).
#
# Single Docker image, profile selected at `docker run` time:
#   docker run aee:2.0.0-rc1.gamma --profile {full,mini,edge,developer}
#
# One image, one codebase, profile selected at run time via the
# docker-entrypoint.sh wrapper. Per §21.5:
#   * --profile edge → AEE_DB_READ_ONLY=1 env var
#   * --profile developer → tempdir DB + smoke test + interactive shell
#
# Compatibility surface (must NOT be modified by this slice):
#   * aee/profiles/descriptor.py  — canonical profile matrix (§21.1)
#   * aee/cli.py                  — unified CLI UX (§21.2)
#   * aee/installer/backend.py    — installer backend (§21.3)
#   * install.sh                  — shell wrapper (§21.3)
#   * dispatcher/db.py            — runtime profile selection (§21.4)
#
# Image tag: aee:2.0.0-rc1.gamma (per §21.M Phase C release label).

FROM python:3.11-slim AS base

# ---------------------------------------------------------------------------
# Install runtime dependencies. The bridge requires:
#   * fastapi, uvicorn[standard], httpx, python-dotenv, pydantic
# AEE-8.x / Epic 9.x plumbing has no extra deps beyond stdlib.
# ---------------------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---------------------------------------------------------------------------
# Copy requirements first for layer caching.
# ---------------------------------------------------------------------------
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Copy the repo. The canonical profile matrix, CLI, installer backend,
# shell wrapper, and dispatcher runtime are all included as-is — this
# slice is purely additive (one Dockerfile + one entrypoint script).
# ---------------------------------------------------------------------------
COPY . /app

# ---------------------------------------------------------------------------
# Install the docker-entrypoint.sh.
# ---------------------------------------------------------------------------
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# ---------------------------------------------------------------------------
# Default entrypoint: the profile-aware wrapper. CMD is empty so
# `docker run aee:X.Y.Z --profile mini` enters smoke-test mode
# (print resolved profile + env vars, exit 0). Pass a command to exec
# it with the env vars set:
#   docker run aee:X.Y.Z --profile full aee --version
# ---------------------------------------------------------------------------
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--profile", "full"]

# ---------------------------------------------------------------------------
# Metadata (OCI image labels).
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="AEE"
LABEL org.opencontainers.image.description="Autonomous Execution Engine — single image, profile selected at run time"
LABEL org.opencontainers.image.version="2.0.0-rc1.gamma"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL aee.profile.supported="full,mini,edge,developer"
LABEL aee.profile.default="full"
