// pi-agent/runtime/tests/test_dry_run.js
// AEE-4 Part B — minimum smoke test for the runtime.
// Runs the runtime with --dry-run on a sample spec, asserts
// the JSON shape, asserts exit code 0.
//
// This test does NOT call any LLM. It exists so the closed-
// loop Pi Worker smoke test (`pi-agent/tests/test_smoke.py`)
// can spawn the runtime with --dry-run and read the
// deterministic output.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const runtime = join(__dirname, "..", "pi-agent-runtime.js");

test("dry-run emits canonical JSON result on stdout and exits 0", () => {
  const tmp = mkdtempSync(join(tmpdir(), "pi-runtime-test-"));
  try {
    const specPath = join(tmp, "spec.json");
    const workdir = join(tmp, "work");
    writeFileSync(
      specPath,
      JSON.stringify({
        job_id: "TASK-TEST-0001",
        input: "echo hello from pi dry-run",
        tools: ["shell", "file_read", "file_write"],
        max_steps: 5,
        per_step_timeout_ms: 5000,
        max_output_bytes: 1024,
        workdir,
        allowlist_cmds: ["ls", "cat", "echo"],
        approval_required: false,
      }),
    );
    const stdout = execFileSync("node", [runtime, "--job-file", specPath, "--dry-run"], {
      encoding: "utf-8",
    });
    // One line of JSON.
    const lines = stdout.trim().split("\n");
    assert.equal(lines.length, 1, `expected 1 line of JSON, got ${lines.length}`);
    const result = JSON.parse(lines[0]);
    assert.equal(result.job_id, "TASK-TEST-0001");
    assert.equal(result.status, "ok");
    assert.match(result.output, /dry-run/);
    assert.equal(result.error, null);
    assert.ok(result.started_at);
    assert.ok(result.finished_at);
    assert.equal(result.finish_reason, "dry_run");
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("invalid spec exits 2 with a JSON error envelope", () => {
  const tmp = mkdtempSync(join(tmpdir(), "pi-runtime-test-"));
  try {
    const specPath = join(tmp, "spec.json");
    writeFileSync(specPath, JSON.stringify({ job_id: "x" })); // missing required fields
    let stdout;
    let code;
    try {
      stdout = execFileSync("node", [runtime, "--job-file", specPath, "--dry-run"], {
        encoding: "utf-8",
      });
    } catch (err) {
      stdout = err.stdout?.toString() || "";
      code = err.status;
    }
    assert.equal(code, 2, `expected exit 2, got ${code}`);
    const result = JSON.parse(stdout.trim().split("\n").pop());
    assert.equal(result.status, "error");
    assert.match(result.error, /spec validation failed/);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("missing job-file flag exits 2 with a usage error", () => {
  let stdout;
  let code;
  try {
    execFileSync("node", [runtime, "--dry-run"], { encoding: "utf-8" });
  } catch (err) {
    stdout = err.stdout?.toString() || "";
    code = err.status;
  }
  assert.ok([2, 8].includes(code), `expected exit 2 or 8, got ${code}`);
});
