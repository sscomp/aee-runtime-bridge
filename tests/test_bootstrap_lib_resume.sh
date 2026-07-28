#!/usr/bin/env bash
# AEE Bootstrap v1 — W6 resume.sh shell integration tests.
#
# Spec §16 W6 deliverable: "POSIX resume-from-last-stage helper".
# Spec §14 testing strategy: integration tests for shell helpers.
#
# These are shell-level integration tests for bootstrap/lib/resume.sh.
# They create temporary marker directories, write marker files, and
# verify that resume_stage / the CLI mode returns the correct stage.
#
# Run: bash tests/test_bootstrap_lib_resume.sh
# Exits 0 if all tests pass, non-zero on first failure.

set -euo pipefail

script_path="${BASH_SOURCE[0]:-$0}"
repo_root="$(cd -P "$(dirname "$script_path")/.." >/dev/null 2>&1 && pwd)"
resume_sh="$repo_root/bootstrap/lib/resume.sh"

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
# Source the helper so we can call resume_stage directly
# ---------------------------------------------------------------------------
# shellcheck source=/dev/null
. "$resume_sh"

# ---------------------------------------------------------------------------
# Test 1: No marker dir → first stage
# ---------------------------------------------------------------------------
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

result="$(resume_stage "$tmpdir/no-such-dir-nonexistent")"
expected="00_detect"
if [ "$result" = "$expected" ]; then
    ok "no marker dir → 00_detect"
else
    fail "no marker dir: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 2: Empty marker dir → first stage
# ---------------------------------------------------------------------------
mkdir -p "$tmpdir/empty"
result="$(resume_stage "$tmpdir/empty")"
expected="00_detect"
if [ "$result" = "$expected" ]; then
    ok "empty marker dir → 00_detect"
else
    fail "empty marker dir: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 3: All stages completed → 'completed'
# ---------------------------------------------------------------------------
mkdir -p "$tmpdir/all-done"
for stage in 00_detect 01_deps 02_clone 03_pin 04_runtime_setup 05_health_check 06_smoke_test 07_agent_ready; do
    printf 'state=completed\n' > "$tmpdir/all-done/$stage"
done
result="$(resume_stage "$tmpdir/all-done")"
expected="completed"
if [ "$result" = "$expected" ]; then
    ok "all completed → completed"
else
    fail "all completed: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 4: All stages skipped → 'completed'
# ---------------------------------------------------------------------------
mkdir -p "$tmpdir/all-skipped"
for stage in 00_detect 01_deps 02_clone 03_pin 04_runtime_setup 05_health_check 06_smoke_test 07_agent_ready; do
    printf 'state=skipped\n' > "$tmpdir/all-skipped/$stage"
done
result="$(resume_stage "$tmpdir/all-skipped")"
expected="completed"
if [ "$result" = "$expected" ]; then
    ok "all skipped → completed"
else
    fail "all skipped: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 5: First stage failed → resume from first stage
# ---------------------------------------------------------------------------
mkdir -p "$tmpdir/first-failed"
printf 'state=failed\n' > "$tmpdir/first-failed/00_detect"
result="$(resume_stage "$tmpdir/first-failed")"
expected="00_detect"
if [ "$result" = "$expected" ]; then
    ok "first stage failed → 00_detect"
else
    fail "first stage failed: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 6: First three completed, fourth failed → resume from fourth
# ---------------------------------------------------------------------------
mkdir -p "$tmpdir/fourth-failed"
printf 'state=completed\n' > "$tmpdir/fourth-failed/00_detect"
printf 'state=completed\n' > "$tmpdir/fourth-failed/01_deps"
printf 'state=completed\n' > "$tmpdir/fourth-failed/02_clone"
printf 'state=failed\n' > "$tmpdir/fourth-failed/03_pin"
result="$(resume_stage "$tmpdir/fourth-failed")"
expected="03_pin"
if [ "$result" = "$expected" ]; then
    ok "fourth stage failed → 03_pin"
else
    fail "fourth stage failed: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 7: First two completed, third missing → resume from third
# ---------------------------------------------------------------------------
mkdir -p "$tmpdir/third-missing"
printf 'state=completed\n' > "$tmpdir/third-missing/00_detect"
printf 'state=completed\n' > "$tmpdir/third-missing/01_deps"
# 02_clone deliberately absent
result="$(resume_stage "$tmpdir/third-missing")"
expected="02_clone"
if [ "$result" = "$expected" ]; then
    ok "third stage missing → 02_clone"
else
    fail "third stage missing: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 8: in_progress treated as needs-rerun
# ---------------------------------------------------------------------------
mkdir -p "$tmpdir/in-progress"
printf 'state=completed\n' > "$tmpdir/in-progress/00_detect"
printf 'state=completed\n' > "$tmpdir/in-progress/01_deps"
printf 'state=in_progress\n' > "$tmpdir/in-progress/02_clone"
result="$(resume_stage "$tmpdir/in-progress")"
expected="02_clone"
if [ "$result" = "$expected" ]; then
    ok "in_progress → 02_clone"
else
    fail "in_progress: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 9: pending (explicit) treated as needs-rerun
# ---------------------------------------------------------------------------
mkdir -p "$tmpdir/pending"
printf 'state=completed\n' > "$tmpdir/pending/00_detect"
printf 'state=pending\n' > "$tmpdir/pending/01_deps"
result="$(resume_stage "$tmpdir/pending")"
expected="01_deps"
if [ "$result" = "$expected" ]; then
    ok "pending → 01_deps"
else
    fail "pending: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 10: Unknown state → conservative resume from that stage
# ---------------------------------------------------------------------------
mkdir -p "$tmpdir/unknown"
printf 'state=completed\n' > "$tmpdir/unknown/00_detect"
printf 'state=bogus\n' > "$tmpdir/unknown/01_deps"
result="$(resume_stage "$tmpdir/unknown")"
expected="01_deps"
if [ "$result" = "$expected" ]; then
    ok "unknown state → 01_deps"
else
    fail "unknown state: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 11: Marker file with extra fields — state= is still found
# ---------------------------------------------------------------------------
mkdir -p "$tmpdir/extra-fields"
printf 'stage=01_deps\nstate=completed\nstarted_at=2026-07-28T10:00:00Z\n' > "$tmpdir/extra-fields/00_detect"
printf 'stage=02_clone\nstate=failed\nstarted_at=2026-07-28T10:05:00Z\n' > "$tmpdir/extra-fields/01_deps"
result="$(resume_stage "$tmpdir/extra-fields")"
expected="01_deps"
if [ "$result" = "$expected" ]; then
    ok "extra fields in marker → 01_deps"
else
    fail "extra fields: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 12: CLI mode --marker-dir
# ---------------------------------------------------------------------------
mkdir -p "$tmpdir/cli-mode"
printf 'state=completed\n' > "$tmpdir/cli-mode/00_detect"
printf 'state=failed\n' > "$tmpdir/cli-mode/01_deps"
result="$(bash "$resume_sh" --marker-dir "$tmpdir/cli-mode")"
expected="01_deps"
if [ "$result" = "$expected" ]; then
    ok "CLI --marker-dir → 01_deps"
else
    fail "CLI --marker-dir: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 13: CLI mode --marker-dir=path
# ---------------------------------------------------------------------------
result="$(bash "$resume_sh" --marker-dir="$tmpdir/cli-mode")"
expected="01_deps"
if [ "$result" = "$expected" ]; then
    ok "CLI --marker-dir=path → 01_deps"
else
    fail "CLI --marker-dir=path: expected '$expected', got '$result'"
fi

# ---------------------------------------------------------------------------
# Test 14: CLI mode --help exits 0
# ---------------------------------------------------------------------------
if bash "$resume_sh" --help >/dev/null 2>&1; then
    ok "CLI --help exits 0"
else
    fail "CLI --help did not exit 0"
fi

# ---------------------------------------------------------------------------
# Test 15: CLI mode unknown arg exits non-zero
# ---------------------------------------------------------------------------
if bash "$resume_sh" --bogus >/dev/null 2>&1; then
    fail "CLI unknown arg did not exit non-zero"
else
    ok "CLI unknown arg exits non-zero"
fi

# ---------------------------------------------------------------------------
# Test 16: STAGE_ORDER has exactly 8 stages
# ---------------------------------------------------------------------------
if [ "${#STAGE_ORDER[@]}" -eq 8 ]; then
    ok "STAGE_ORDER has 8 stages"
else
    fail "STAGE_ORDER has ${#STAGE_ORDER[@]} stages (expected 8)"
fi

# ---------------------------------------------------------------------------
# Test 17: STAGE_ORDER matches lifecycle.StageName
# ---------------------------------------------------------------------------
python_check="$tmpdir/check_stages.py"
cat > "$python_check" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
from aee.installer.lifecycle import StageName
stages = [s.value for s in StageName]
shell_stages = sys.argv[2].split(",")
if stages == shell_stages:
    print("MATCH")
else:
    print(f"MISMATCH: python={stages} shell={shell_stages}")
PYEOF
py_result="$(python3 "$python_check" "$repo_root" "$(IFS=,; echo "${STAGE_ORDER[*]}")")"
if [ "$py_result" = "MATCH" ]; then
    ok "STAGE_ORDER matches lifecycle.StageName"
else
    fail "STAGE_ORDER vs lifecycle mismatch: $py_result"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "resume.sh tests: ${pass_count} passed, ${fail_count} failed"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi
exit 0