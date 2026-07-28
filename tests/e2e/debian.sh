#!/usr/bin/env bash
# AEE Bootstrap v1 — W11 Debian E2E harness (spec §16 W11, §14 testing strategy).
#
# Container E2E harness for Debian. Mirrors ubuntu.sh — the Phase B
# surface is platform-agnostic shell + Python; the only Debian-specific
# difference is the apt manifest path (already covered by apt.deps.txt
# used for both Ubuntu and Debian per spec §5.5).
#
# What this harness DOES (in this environment):
#   1. Verifies the Phase B surface is present on disk.
#   2. Runs the shell integration tests (detect, deps, resume).
#   3. Runs the Python integration tests (redaction + resume + stage).
#   4. Reports a summary line: "debian-e2e: N passed, M failed".
#
# What this harness DOES NOT do:
#   * Spin up a Debian container (no Docker-in-Docker on Abacus).
#   * Perform a real apt install (deps.sh --execute requires sudo).
#
# Run: bash tests/e2e/debian.sh
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
echo "# Debian E2E harness — Phase B surface presence"

for f in \
    bootstrap/lib/detect.sh \
    bootstrap/lib/deps.sh \
    bootstrap/lib/resume.sh \
    bootstrap/manifests/apt.deps.txt \
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

if bash "$repo_root/tests/test_bootstrap_lib_detect.sh" >/tmp/e2e-debian-detect.log 2>&1; then
    ok "detect.sh shell tests pass"
else
    fail "detect.sh shell tests failed (see /tmp/e2e-debian-detect.log)"
fi

if bash "$repo_root/tests/test_bootstrap_lib_deps.sh" >/tmp/e2e-debian-deps.log 2>&1; then
    ok "deps.sh shell tests pass"
else
    fail "deps.sh shell tests failed (see /tmp/e2e-debian-deps.log)"
fi

if bash "$repo_root/tests/test_bootstrap_lib_resume.sh" >/tmp/e2e-debian-resume.log 2>&1; then
    ok "resume.sh shell tests pass"
else
    fail "resume.sh shell tests failed (see /tmp/e2e-debian-resume.log)"
fi

# ---------------------------------------------------------------------------
# 3. Python integration tests
# ---------------------------------------------------------------------------
echo "# Python integration tests"

if PYTHONPATH="$repo_root" python3 -m unittest aee.tests.test_bootstrap_integration >/tmp/e2e-debian-py.log 2>&1; then
    ok "test_bootstrap_integration.py passes"
else
    fail "test_bootstrap_integration.py failed (see /tmp/e2e-debian-py.log)"
fi

# ---------------------------------------------------------------------------
# 4. Summary
# ---------------------------------------------------------------------------
echo ""
echo "debian-e2e: ${pass_count} passed, ${fail_count} failed"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0