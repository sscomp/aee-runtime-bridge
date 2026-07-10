// pi-agent/runtime/lib/spec.js
// Zod schema + parse helper for the job spec the daemon writes to
// the spec file before invoking the runtime.
//
// The spec is the runtime-neutral contract between the AEE-4
// Pi Worker daemon and the LLM loop. The daemon writes a single
// JSON object to the spec file; the runtime reads it, validates
// the shape with zod, and either proceeds or exits with code 2
// (invalid job spec).
//
// Field semantics mirror the AEE-4 Worker Runtime Contract
// (`docs/runtime/Worker_Runtime_Contract.md`):
//   * job_id          — the TASK-YYYYMMDD-NNNN id the daemon
//                       received from /v1/jobs/claim. Used in
//                       every log line and the result JSON.
//   * input           — the user instruction. The LLM sees this
//                       as the first user message.
//   * tools           — subset of ["shell", "file_read",
//                       "file_write"]. Tools the runtime will
//                       expose to the LLM. The runtime errors
//                       (exit 7) if the LLM tries to call a
//                       tool not in this list.
//   * max_steps       — hard cap on LLM iterations. Default 20.
//                       When reached without `finish_reason=stop`,
//                       the runtime exits with code 8 (internal).
//   * per_step_timeout_ms — per-tool-call wall-clock timeout.
//                       Default 30000 ms.
//   * max_output_bytes — stdout / stderr cap per tool call.
//                       Default 204800 (200 KB).
//   * workdir         — the per-job workdir. All shell / file
//                       paths MUST resolve inside this dir; the
//                       runtime rejects with exit 6 otherwise.
//   * allowlist_cmds  — list of allowed first tokens for the
//                       `shell` tool. Default mirrors the
//                       allowlist_commands in pi-agent/config.yaml.
//   * approval_required — placeholder for AEE-5+ human-in-the-loop
//                       approval. AEE-4 reads the field but
//                       does not act on it (always allow).
//
// The daemon is the single source of truth for these values;
// the runtime is a slave. A malformed spec is a daemon bug,
// not a runtime bug, so the runtime exits 2 (not 0) and the
// daemon fails the job on the bridge.

import { z } from "zod";

export const SpecSchema = z.object({
  job_id: z.string().min(1),
  input: z.string().min(1),
  tools: z.array(z.enum(["shell", "file_read", "file_write"])).default(["shell", "file_read", "file_write"]),
  max_steps: z.number().int().positive().max(100).default(20),
  per_step_timeout_ms: z.number().int().positive().max(600000).default(30000),
  max_output_bytes: z.number().int().positive().max(10_485_760).default(204_800), // 10 MB hard cap
  workdir: z.string().min(1),
  allowlist_cmds: z.array(z.string()).default([]),
  approval_required: z.boolean().default(false),
});

export function parseSpec(raw) {
  // Parse + validate. On failure, throw a structured error that
  // the CLI catches and converts to exit code 2.
  let obj;
  try {
    obj = JSON.parse(raw);
  } catch (err) {
    const e = new Error(`spec is not valid JSON: ${err.message}`);
    e.exitCode = 2;
    throw e;
  }
  const result = SpecSchema.safeParse(obj);
  if (!result.success) {
    const issues = result.error.issues
      .map((i) => `${i.path.join(".") || "(root)"}: ${i.message}`)
      .join("; ");
    const e = new Error(`spec validation failed: ${issues}`);
    e.exitCode = 2;
    throw e;
  }
  return result.data;
}
