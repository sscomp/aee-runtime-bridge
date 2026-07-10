#!/usr/bin/env node
// aee-runtime/runtime/aee-runtime.js
// AEE-4 Part B — AEE Lightweight Agent Runtime (Node.js, in-house).
//
// Reads a JSON job spec from a file (--job-file) or stdin
// (--job-stdin), optionally calls an OpenAI-compatible LLM,
// optionally executes shell / file_read / file_write tools,
// prints a single JSON result on stdout, exits 0.
//
// Exit codes (the daemon maps these to bridge /fail calls):
//   0  success — single JSON result on stdout
//   2  invalid job spec (parse / zod validation failure)
//   3  provider failure (no API key, bad base URL, network)
//   4  per-step timeout (a tool call exceeded
//      per_step_timeout_ms; the loop continues but the
//      failing tool gets exit_code 4 baked into the JSON)
//   5  allowlist blocked (the LLM tried to call shell with
//      a binary not in allowlist_cmds)
//   6  workdir violation (file tool resolved outside workdir)
//   7  unknown tool (the LLM hallucinated a tool name)
//   8  internal error (uncaught exception)
//   9  lease expired (the runtime was killed by the daemon's
//      timeout; no JSON printed)
//
// The dry-run path (--dry-run) exits 0 and prints a canned
// response; it's how the smoke test exercises the daemon
// without a real LLM.

import { Command } from "commander";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { config as loadDotenv } from "dotenv";
import { parseSpec } from "./lib/spec.js";
import { makeProvider } from "./lib/provider.js";
import { runLoop } from "./lib/loop.js";
import { emitResult, dryRunResult } from "./lib/result.js";

const EXIT = {
  OK: 0,
  BAD_SPEC: 2,
  PROVIDER: 3,
  TIMEOUT: 4,
  ALLOWLIST: 5,
  WORKDIR: 6,
  UNKNOWN_TOOL: 7,
  INTERNAL: 8,
  LEASE: 9,
};

function die(exitCode, msg) {
  // Error path: a single JSON object on stdout, same shape as
  // the success path, with status="error" and error=msg. This
  // means the daemon always sees one JSON object regardless of
  // success / failure, and can decide at the higher level.
  process.stdout.write(
    JSON.stringify({
      job_id: "(unknown)",
      status: "error",
      output: null,
      tool_calls: [],
      usage: null,
      started_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
      finish_reason: "error",
      error: msg,
    }) + "\n",
  );
  process.exit(exitCode);
}

async function readSpec(opts) {
  if (opts.jobFile && opts.jobStdin) {
    die(EXIT.BAD_SPEC, "specify exactly one of --job-file or --job-stdin");
  }
  let raw;
  if (opts.jobFile) {
    try {
      raw = readFileSync(resolve(opts.jobFile), "utf-8");
    } catch (err) {
      die(EXIT.BAD_SPEC, `cannot read --job-file: ${err.message}`);
    }
  } else if (opts.jobStdin) {
    raw = await new Promise((res, rej) => {
      let buf = "";
      process.stdin.setEncoding("utf-8");
      process.stdin.on("data", (c) => (buf += c));
      process.stdin.on("end", () => res(buf));
      process.stdin.on("error", rej);
    });
  } else {
    die(EXIT.BAD_SPEC, "specify --job-file PATH or --job-stdin");
  }
  try {
    return parseSpec(raw);
  } catch (err) {
    die(EXIT.BAD_SPEC, err.message);
  }
}

const program = new Command();
program
  .name("aee-runtime")
  .description("AEE-4 Part B — AEE Lightweight Agent Runtime (Node.js)")
  .option("--job-file <path>", "path to the JSON job spec file")
  .option("--job-stdin", "read the job spec from stdin instead of --job-file")
  .option("--provider-base-url <url>", "OpenAI-compatible base URL", process.env.PI_PROVIDER_BASE_URL)
  .option("--provider-api-key <key>", "OpenAI-compatible API key", process.env.PI_PROVIDER_API_KEY)
  .option("--provider-model <name>", "model name", process.env.PI_PROVIDER_MODEL)
  .option("--allowlist-cmds <list>", "comma-separated allowlist (overrides spec)", (v) => v.split(",").map((s) => s.trim()).filter(Boolean))
  .option("--workdir <path>", "per-job workdir (overrides spec)")
  .option("--max-steps <n>", "max LLM iterations (overrides spec)", (v) => parseInt(v, 10))
  .option("--per-step-timeout-ms <n>", "per-tool-call timeout in ms (overrides spec)", (v) => parseInt(v, 10))
  .option("--max-output-bytes <n>", "stdout/stderr cap per tool call in bytes (overrides spec)", (v) => parseInt(v, 10))
  .option("--dry-run", "skip the LLM call; emit a canned response and exit 0")
  .option("--env-file <path>", "path to a dotenv file with PI_PROVIDER_* vars (alternative to --provider-*)");

program.parseAsync(process.argv).then(async () => {
  const opts = program.opts();
  if (opts.envFile) {
    loadDotenv({ path: resolve(opts.envFile) });
    // Re-read after dotenv load. Commander already consumed the
    // env defaults at parse-time; we have to override here.
    opts.providerBaseUrl = opts.providerBaseUrl || process.env.PI_PROVIDER_BASE_URL;
    opts.providerApiKey = opts.providerApiKey || process.env.PI_PROVIDER_API_KEY;
    opts.providerModel = opts.providerModel || process.env.PI_PROVIDER_MODEL;
  }
  const spec = await readSpec(opts);
  // CLI flags override spec fields when both are present.
  if (opts.workdir) spec.workdir = opts.workdir;
  if (opts.maxSteps) spec.max_steps = opts.maxSteps;
  if (opts.perStepTimeoutMs) spec.per_step_timeout_ms = opts.perStepTimeoutMs;
  if (opts.maxOutputBytes) spec.max_output_bytes = opts.maxOutputBytes;
  if (opts.allowlistCmds && opts.allowlistCmds.length > 0) {
    spec.allowlist_cmds = opts.allowlistCmds;
  }

  if (opts.dryRun) {
    emitResult(dryRunResult({ spec }));
    process.exit(EXIT.OK);
  }

  let provider;
  try {
    provider = makeProvider({
      base_url: opts.providerBaseUrl,
      api_key: opts.providerApiKey,
      model: opts.providerModel,
    });
  } catch (err) {
    die(EXIT.PROVIDER, err.message);
  }

  try {
    const result = await runLoop({ spec, provider });
    emitResult(result);
    // Map loop-level errors to the runtime's exit codes. The
    // loop sets result.error; the daemon reads result.status
    // and result.error and decides what to call on the bridge.
    if (result.status === "error") {
      // Inspect the tool_calls for a hint of which exit code
      // the daemon should treat this as. The most common case
      // is a tool returning exit_code 5/6/7; we surface those
      // as the runtime's exit code so the daemon can log them
      // more precisely.
      let code = EXIT.INTERNAL;
      const last = result.tool_calls?.[result.tool_calls.length - 1];
      if (last?.result?.exit_code === 5) code = EXIT.ALLOWLIST;
      else if (last?.result?.exit_code === 6) code = EXIT.WORKDIR;
      else if (last?.result?.exit_code === 7) code = EXIT.UNKNOWN_TOOL;
      else if (last?.result?.exit_code === 4 || last?.result?.timeout) code = EXIT.TIMEOUT;
      process.exit(code);
    }
    process.exit(EXIT.OK);
  } catch (err) {
    die(EXIT.INTERNAL, `internal error: ${err.message || err}`);
  }
});
