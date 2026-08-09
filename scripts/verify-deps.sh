#!/usr/bin/env bash
# verify-deps.sh — Read-only verification of lockfile integrity.
# Does NOT mutate the venv, does NOT install anything.
# Run from repo root (or this script cd's to repo root automatically).
set -euo pipefail
cd "$(dirname "$0")/.."

FAIL=0

echo "=== 1. Lockfile existence ==="
for f in requirements.lock requirements-dev.lock requirements.lock.darwin constraints.txt requirements.in requirements-dev.in; do
  if [ -s "$f" ]; then
    echo "  OK: $f ($(wc -c < "$f") bytes)"
  else
    echo "  FAIL: $f missing or empty"
    FAIL=1
  fi
done

echo "=== 2. Hash presence ==="
LINUX_HASHES=$(grep -c -- '--hash=sha256:' requirements.lock || true)
DEV_HASHES=$(grep -c -- '--hash=sha256:' requirements-dev.lock || true)
DARWIN_HASHES=$(grep -c -- '--hash=sha256:' requirements.lock.darwin || true)
echo "  requirements.lock:        ${LINUX_HASHES} hash lines"
echo "  requirements-dev.lock:    ${DEV_HASHES} hash lines"
echo "  requirements.lock.darwin: ${DARWIN_HASHES} hash lines"
[ "$LINUX_HASHES" -ge 5 ] || { echo "  FAIL: too few hashes in requirements.lock"; FAIL=1; }
[ "$DEV_HASHES" -ge 5 ] || { echo "  FAIL: too few hashes in requirements-dev.lock"; FAIL=1; }
[ "$DARWIN_HASHES" -ge 5 ] || { echo "  FAIL: too few hashes in requirements.lock.darwin"; FAIL=1; }

echo "=== 3. Lockfile metadata (generation command header) ==="
for f in requirements.lock requirements-dev.lock requirements.lock.darwin; do
  HEADER=$(head -5 "$f" | grep '^#.*uv pip compile' || true)
  if [ -n "$HEADER" ]; then
    echo "  OK: $f has generation header"
  else
    echo "  FAIL: $f missing generation header"
    FAIL=1
  fi
  # Check Python version pin (in the generation command line)
  if head -5 "$f" | grep -q 'python-version 3.11'; then
    echo "  OK: $f pins python-version 3.11"
  else
    echo "  FAIL: $f missing python-version 3.11"
    FAIL=1
  fi
done

echo "=== 4. uv pip sync --dry-run (runtime lock) ==="
if uv pip sync --dry-run requirements.lock 2>&1 | head -30; then
  echo "  OK: dry-run sync passed"
else
  echo "  NOTE: dry-run sync reported changes (expected if venv differs from lock)"
  # dry-run exit code != 0 means it WOULD make changes; not necessarily a failure
fi

echo "=== 5. Import smoke test (runtime deps in .venv) ==="
if .venv/bin/python -c "import fastapi, uvicorn, httpx, pydantic, dotenv; print('runtime imports ok')" 2>&1; then
  echo "  OK: runtime imports succeeded"
else
  echo "  FAIL: runtime imports failed"
  FAIL=1
fi

echo "=== 6. Dev import smoke test ==="
if .venv/bin/python -c "import pytest, yaml; print('dev imports ok')" 2>&1; then
  echo "  OK: dev imports succeeded"
else
  echo "  FAIL: dev imports failed"
  FAIL=1
fi

echo "=== 7. AEE unittest smoke (stdlib-only, no deps needed) ==="
if PYTHONPATH=. .venv/bin/python -m unittest discover -s aee/tests -p 'test_aee9*.py' -v 2>&1 | tail -10; then
  echo "  OK: AEE unittest suite passed"
else
  echo "  FAIL: AEE unittest suite had failures"
  FAIL=1
fi

echo "=== 8. Lockfile syntax check (pip-compile format) ==="
# Verify every package line has at least one hash
NO_HASH=$(grep -v '^#' requirements.lock | grep -v '^$' | grep -v '\\$' | grep -v '    --hash=' | grep -v '^    #' || true)
if [ -z "$NO_HASH" ]; then
  echo "  OK: all package lines have hashes"
else
  echo "  FAIL: lines without hashes:"
  echo "$NO_HASH"
  FAIL=1
fi

echo "=== 9. No secrets / no absolute paths in lockfiles ==="
if grep -qE '(/home/|/Users/|API_KEY|TOKEN|SECRET|PASSWORD)' requirements.lock requirements-dev.lock requirements.lock.darwin 2>/dev/null; then
  echo "  FAIL: found secret-like or absolute-path content in lockfile"
  FAIL=1
else
  echo "  OK: no secrets or absolute paths found"
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "=== VERDICT: ALL CHECKS PASSED ==="
  exit 0
else
  echo "=== VERDICT: SOME CHECKS FAILED ==="
  exit 1
fi