#!/usr/bin/env bash
# compile-deps.sh — Regenerate hash-pinned lockfiles using uv pip compile.
# Idempotent: running again produces the same output (given unchanged inputs).
#
# Prerequisites:
#   - uv >= 0.11.8 installed and on PATH
#   - Run from repo root (or this script cd's to repo root automatically)
#
# Produces:
#   requirements.lock         — Linux x86_64, Python 3.11, runtime deps
#   requirements-dev.lock     — Linux x86_64, Python 3.11, runtime + dev deps
#   requirements.lock.darwin  — macOS arm64, Python 3.11, runtime deps (cross-compile)
#
# Usage:
#   bash scripts/compile-deps.sh
set -euo pipefail
cd "$(dirname "$0")/.."

UV_VERSION_REQUIRED="0.11.8"
PYVER="3.11"
LINUX_PLATFORM="x86_64-unknown-linux-gnu"
DARWIN_PLATFORM="aarch64-apple-darwin"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] uv: $(uv --version)"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Python target: ${PYVER}"

# --- Runtime lock (Linux) ---
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Compiling requirements.lock (linux x86_64)..."
uv pip compile \
  --python-version "${PYVER}" \
  --python-platform "${LINUX_PLATFORM}" \
  --generate-hashes \
  -c constraints.txt \
  -o requirements.lock \
  requirements.in

# --- Dev lock (Linux) ---
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Compiling requirements-dev.lock (linux x86_64)..."
uv pip compile \
  --python-version "${PYVER}" \
  --python-platform "${LINUX_PLATFORM}" \
  --generate-hashes \
  -c constraints.txt \
  -o requirements-dev.lock \
  requirements-dev.in

# --- Darwin lock (macOS arm64, cross-compile) ---
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Compiling requirements.lock.darwin (macOS arm64)..."
uv pip compile \
  --python-version "${PYVER}" \
  --python-platform "${DARWIN_PLATFORM}" \
  --generate-hashes \
  -c constraints.txt \
  -o requirements.lock.darwin \
  requirements.in

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Done. Lockfiles generated:"
ls -la requirements.lock requirements-dev.lock requirements.lock.darwin