// aee-runtime/runtime/lib/tools.js
// Three tool handlers: shell, file_read, file_write.
// Each enforces workdir containment + per-call timeout + output cap.
// Returns {ok, output, error?, duration_ms} so the loop can serialize
// the result and feed it back into the LLM message stream.
//
// Failure modes (all return ok=false; the LLM sees the error):
//   * shell          — exit code 5 (allowlist), 4 (timeout), or
//                      non-zero exit (1)
//   * file_read/write — exit 6 (workdir violation)
//
// The CLI's job is to map these to bridge calls; the runtime
// itself never decides "is this a job-failing error" — it
// only reports what happened. The daemon maps the exit code
// at the higher level (see aee-runtime/aee_runtime.py).

import { exec } from "node:child_process";
import { readFile, writeFile, stat } from "node:fs/promises";
import { resolve, relative } from "node:path";
import { promisify } from "node:util";

const execAsync = promisify(exec);

function isInsideWorkdir(p, workdir) {
  // Path safety: `resolved` must equal `workdir` or be a
  // descendant of it. We use a path-relative check so a
  // `workdir` of `/a` does not let a child escape into
  // `/abc/...` (a common prefix bug).
  const r = resolve(p);
  const w = resolve(workdir);
  const rel = relative(w, r);
  return !rel.startsWith("..") && !resolve(w, rel).startsWith("..");
}

function truncate(s, max) {
  if (typeof s !== "string") return "";
  if (s.length <= max) return s;
  return s.slice(0, max) + `\n[...truncated, ${s.length - max} bytes omitted...]`;
}

function firstToken(cmd) {
  // Match the same shlex-style split as `dispatcher.safety._first_token`.
  // We do not pull in a shlex library; the allowlist is on the binary
  // name, which is the first whitespace-delimited token.
  const trimmed = cmd.trim();
  if (!trimmed) return "";
  return trimmed.split(/\s+/)[0];
}

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms),
    ),
  ]);
}

// --- shell tool ------------------------------------------------------------

export async function runShell({ cmd, workdir, allowlist_cmds, per_step_timeout_ms, max_output_bytes }) {
  const start = Date.now();
  // Allowlist check on the first token. Mirrors the AEE-2 safety
  // policy (config/safety.json) so the runtime-side and the
  // daemon-side agree on what's allowed. The first-token rule is
  // deliberately simple; full safety.evaluate() is the daemon's
  // job (defence in depth).
  const binary = firstToken(cmd);
  if (binary && allowlist_cmds && allowlist_cmds.length > 0 && !allowlist_cmds.includes(binary)) {
    return {
      ok: false,
      output: "",
      error: `command not in allowlist: ${binary}`,
      duration_ms: Date.now() - start,
      exit_code: 5,
    };
  }
  try {
    const { stdout, stderr } = await withTimeout(
      execAsync(cmd, {
        cwd: workdir,
        maxBuffer: max_output_bytes,
        timeout: per_step_timeout_ms,
        // Inherit env minus any *_API_KEY the daemon set; the
        // runtime does not need them. (The daemon's process env
        // does not include them, so this is a no-op safety
        // measure — but explicit > implicit.)
        env: { PATH: process.env.PATH || "", HOME: process.env.HOME || "" },
      }),
      per_step_timeout_ms + 2000, // give exec + cleanup a 2s grace
      "shell",
    );
    return {
      ok: true,
      output: truncate(`${stdout}${stderr ? `\n[stderr]\n${stderr}` : ""}`, max_output_bytes),
      duration_ms: Date.now() - start,
      exit_code: 0,
    };
  } catch (err) {
    // exec errors carry a `code` (signal or non-zero) and the
    // captured stdout/stderr in the error object on some Node
    // versions. We treat any thrown error as ok=false; the LLM
    // gets the message and can retry.
    const out = err.stdout ? String(err.stdout) : "";
    const errOut = err.stderr ? String(err.stderr) : "";
    return {
      ok: false,
      output: truncate(`${out}${errOut ? `\n[stderr]\n${errOut}` : ""}`, max_output_bytes),
      error: String(err.message || err),
      duration_ms: Date.now() - start,
      exit_code: typeof err.code === "number" ? err.code : 1,
      timeout: err.killed && err.signal === "SIGTERM",
    };
  }
}

// --- file_read tool --------------------------------------------------------

export async function readFileTool({ path, workdir, max_output_bytes }) {
  const start = Date.now();
  if (!isInsideWorkdir(path, workdir)) {
    return {
      ok: false,
      output: "",
      error: `path escapes workdir: ${path}`,
      duration_ms: Date.now() - start,
      exit_code: 6,
    };
  }
  try {
    const s = await stat(path);
    if (!s.isFile()) {
      return {
        ok: false,
        output: "",
        error: `not a regular file: ${path}`,
        duration_ms: Date.now() - start,
        exit_code: 6,
      };
    }
    if (s.size > max_output_bytes) {
      return {
        ok: false,
        output: "",
        error: `file too large: ${s.size} > ${max_output_bytes}`,
        duration_ms: Date.now() - start,
        exit_code: 6,
      };
    }
    const content = await readFile(path, "utf-8");
    return {
      ok: true,
      output: content,
      duration_ms: Date.now() - start,
      exit_code: 0,
    };
  } catch (err) {
    return {
      ok: false,
      output: "",
      error: `file_read failed: ${err.message}`,
      duration_ms: Date.now() - start,
      exit_code: 1,
    };
  }
}

// --- file_write tool -------------------------------------------------------

export async function writeFileTool({ path, content, workdir }) {
  const start = Date.now();
  if (!isInsideWorkdir(path, workdir)) {
    return {
      ok: false,
      output: "",
      error: `path escapes workdir: ${path}`,
      duration_ms: Date.now() - start,
      exit_code: 6,
    };
  }
  try {
    await writeFile(path, content, "utf-8");
    return {
      ok: true,
      output: `wrote ${content.length} bytes to ${path}`,
      duration_ms: Date.now() - start,
      exit_code: 0,
    };
  } catch (err) {
    return {
      ok: false,
      output: "",
      error: `file_write failed: ${err.message}`,
      duration_ms: Date.now() - start,
      exit_code: 1,
    };
  }
}

// --- tool dispatcher -------------------------------------------------------

export async function dispatchTool({ name, args, ctx }) {
  switch (name) {
    case "shell":
      return runShell({ cmd: args.cmd, ...ctx });
    case "file_read":
      return readFileTool({ path: args.path, ...ctx });
    case "file_write":
      return writeFileTool({ path: args.path, content: args.content, ...ctx });
    default:
      return {
        ok: false,
        output: "",
        error: `unknown tool: ${name}`,
        duration_ms: 0,
        exit_code: 7,
      };
  }
}

// --- tool defs (for chat.completions tools=) --------------------------------

export const TOOL_DEFS = [
  {
    type: "function",
    function: {
      name: "shell",
      description: "Run a shell command inside the per-job workdir. The first token must be in the allowlist. stdout + stderr are returned; both are truncated at max_output_bytes.",
      parameters: {
        type: "object",
        properties: {
          cmd: { type: "string", description: "The shell command to run." },
        },
        required: ["cmd"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "file_read",
      description: "Read a file inside the per-job workdir. The path must not escape the workdir.",
      parameters: {
        type: "object",
        properties: {
          path: { type: "string", description: "The file path, relative to the workdir or absolute." },
        },
        required: ["path"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "file_write",
      description: "Write content to a file inside the per-job workdir. Overwrites if the file exists.",
      parameters: {
        type: "object",
        properties: {
          path: { type: "string" },
          content: { type: "string" },
        },
        required: ["path", "content"],
      },
    },
  },
];
