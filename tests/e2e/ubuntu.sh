#!/usr/bin/env bash
# AEE Bootstrap v1 — W11 Ubuntu E2E harness (spec §16 W11, §14 testing strategy).
#
# Container E2E harness for Ubuntu. This is a *harness shell* — it
# documents and validates the test plan for an Ubuntu container E2E run
# without claiming to actually spin up a container in this environment
# (the Abacus host does not allow Docker-in-Docker; spec §13.1 assumes
# an Ubuntu host or container reachable via SSH/shell).
#
# Spec §16 W11 deliverable: "Container E2E harness (Ubuntu, Debian)".
# Spec §14 testing strategy: integration tests + E2E + acceptance gate.
#
# What this harness DOES (in this environment):
#   1. Verifies the bootstrap v1 Phase B surface is present on disk:
#      - bootstrap/lib/{detect,deps,resume}.sh
#      - bootstrap/manifests/{apt,brew,python.requirements}.{deps.txt,in,lock}
#      - aee/installer/redaction.py
#      - aee/tests/test_bootstrap_integration.py
#   2. Runs the shell integration tests (detect, deps, resume).
#   3. Runs the Python integration tests (redaction + resume + stage).
#   4. Reports a summary line: "ubuntu-e2e: N passed, M failed".
#
# What this harness DOES NOT do:
#   * Spin up an Ubuntu container (no Docker-in-Docker on Abacus).
#   * Perform a real apt install (deps.sh --execute requires sudo).
#   * Perform a real git clone (network + auth not configured here).
#
# The harness is honest about this: it exits 0 only when the on-host
# Phase B surface + unit/shell tests all pass. A real container E2E
# would extend this script with `docker run ubuntu:22.04 ...` steps;
# that is a CI-runner responsibility, not a Phase B deliverable.
#
# Run: bash tests/e2e/ubuntu.sh
# Exits 0 if all checks pass, non-zero on first failure.

set -euo pipefail

script_path="${BASH_SOURCE[0]:-$0}"
repo_root="$(cd -P "$(dirname "$script_path")/../.." >/dev/null 2>&1 && pwd)"

pass_count=0
fail_count=0

ok() {
    echo "ok - $1"
    pass_count=$((pass_count + 1))
}

fail() {
    echo "not ok - $1"
    fail_count=$((fail_count + 1))
}

# ---------------------------------------------------------------------------
# 1. Phase B surface presence check
# ---------------------------------------------------------------------------
echo "# Ubuntu E2E harness — Phase B surface presence"

for f in \
    bootstrap/lib/detect.sh \
    bootstrap/lib/deps.sh \
    bootstrap/lib/resume.sh \
    bootstrap/manifests/apt.deps.txt \
    bootstrap/manifests/brew.deps.txt \
    bootstrap/manifests/python.requirements.in \
    bootstrap/manifests/python.requirements.lock \
    aee/installer/redaction.py \
    aee/tests/test_bootstrap_integration.py
do
    if [ -f "$repo_root/$f" ]; then
        ok "present: $f"
    else
        fail "missing: $f"
    fi
done

# ---------------------------------------------------------------------------
# 2. Shell integration tests
# ---------------------------------------------------------------------------
echo "# Shell integration tests"

if bash "$repo_root/tests/test_bootstrap_lib_detect.sh" >/tmp/e2e-ubuntu-detect.log 2>&1; then
    ok "detect.sh shell tests pass"
else
    fail "detect.sh shell tests failed (see /tmp/e2e-ubuntu-detect.log)"
fi

if bash "$repo_root/tests/test_bootstrap_lib_deps.sh" >/tmp/e2e-ubuntu-deps.log 2>&1; then
    ok "deps.sh shell tests pass"
else
    fail "deps.sh shell tests failed (see /tmp/e2e-ubuntu-deps.log)"
fi

if bash "$repo_root/tests/test_bootstrap_lib_resume.sh" >/tmp/e2e-ubuntu-resume.log 2>&1; then
    ok "resume.sh shell tests pass"
else
    fail "resume.sh shell tests failed (see /tmp/e2e-ubuntu-resume.log)"
fi

# ---------------------------------------------------------------------------
# 3. Python integration tests
# ---------------------------------------------------------------------------
echo "# Python integration tests"

if PYTHONPATH="$repo_root" python3 -m unittest aee.tests.test_bootstrap_integration >/tmp/e2e-ubuntu-py.log 2>&1; then
    ok "test_bootstrap_integration.py passes"
else
    fail "test_bootstrap_integration.py failed (see /tmp/e2e-ubuntu-py.log)"
fi

# ---------------------------------------------------------------------------
# 4. Summary
# ---------------------------------------------------------------------------
echo ""
echo "ubuntu-e2e: ${pass_count} passed, ${fail_count} failed"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0