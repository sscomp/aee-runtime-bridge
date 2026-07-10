// pi-agent/runtime/lib/result.js
// Wraps the loop output into the canonical result JSON and
// writes it to stdout. The daemon reads this single JSON
// object; anything else on stdout is treated as a runtime
// bug (the daemon logs the garbage and fails the job).
//
// The shape mirrors the AEE-4 contract: a single JSON object
// with `job_id`, `status`, `output`, `tool_calls`, `usage`,
// `started_at`, `finished_at`, plus optional `error` and
// `finish_reason` for observability.

export function emitResult(result) {
  // One JSON object, one line. Newlines inside string fields
  // are escaped by JSON.stringify. This is the contract: the
  // daemon reads exactly one JSON object from stdout.
  process.stdout.write(JSON.stringify(result) + "\n");
}

// The "dry-run" canned response. Used by --dry-run mode and
// the smoke test; the daemon can call the runtime with
// --dry-run and a sample spec to get a deterministic result
// without an LLM call.
export function dryRunResult({ spec }) {
  return {
    job_id: spec.job_id,
    status: "ok",
    output: `[dry-run] would execute: ${spec.input}`,
    tool_calls: [],
    usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    started_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
    finish_reason: "dry_run",
    error: null,
  };
}
