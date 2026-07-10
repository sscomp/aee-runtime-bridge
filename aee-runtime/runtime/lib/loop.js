// aee-runtime/runtime/lib/loop.js
// The function-calling loop. Up to `max_steps` iterations:
//   1. Call the LLM with the current messages + tool defs.
//   2. If finish_reason === "stop", return the assistant's
//      final message as the answer.
//   3. If finish_reason === "tool_calls", run each tool
//      call sequentially; append the tool results to the
//      messages; loop.
//   4. On any other finish_reason, return the partial answer
//      with a warning.
//
// Each step is timed; the loop returns the canonical
// `{job_id, status, output, tool_calls, usage, started_at,
//  finished_at, error?}` shape that the daemon reads.
//
// "Output" semantics: when the LLM returns a final message
// (`finish_reason === "stop"`), `output` is the assistant's
// `content` string. When the loop runs out of steps, `output`
// is the last assistant's content and `error` is set.

import { callProvider } from "./provider.js";
import { dispatchTool, TOOL_DEFS } from "./tools.js";

export async function runLoop({ spec, provider, logger = () => {} }) {
  const started_at = new Date().toISOString();
  const messages = [
    {
      role: "system",
      content: [
        "You are the AEE Lightweight Agent Runtime, a careful and concise worker that executes a single user instruction.",
        "You operate inside a per-job workdir. Every shell command, file read, and file write must be inside that workdir.",
        "The shell tool's first token must be in the allowlist. If a command is not in the allowlist, do not call shell with it; either rewrite the command to use an allowed binary, or return your final answer noting the limitation.",
        "Prefer reading files before writing them. Prefer running one shell command at a time and observing the output before issuing the next.",
        "When the user instruction is complete, return a final assistant message with no tool calls (finish_reason='stop'). That final message is what the user sees.",
        `The per-job workdir is: ${spec.workdir}`,
        `The allowed shell binaries are: ${spec.allowlist_cmds.join(", ")}`,
      ].join("\n"),
    },
    { role: "user", content: spec.input },
  ];
  // Filter tool defs to only those the spec requests. Defense in
  // depth: even if the LLM hallucinates a tool not in `spec.tools`,
  // the dispatcher would return exit_code 7, so this is just a
  // tidy guard for the request itself.
  const toolDefs = TOOL_DEFS.filter((t) => spec.tools.includes(t.function.name));

  const tool_calls = [];
  let usage = null;
  let finalContent = null;
  let lastFinishReason = null;
  let loopError = null;

  for (let step = 0; step < spec.max_steps; step++) {
    let response;
    try {
      response = await callProvider({
        client: provider.client,
        model: provider.model,
        messages,
        tools: toolDefs,
      });
    } catch (err) {
      loopError = `provider failure: ${err.message || err}`;
      break;
    }
    if (response.usage) {
      usage = response.usage;
    }
    const choice = response.choices?.[0];
    if (!choice) {
      loopError = "provider returned no choices";
      break;
    }
    lastFinishReason = choice.finish_reason;
    const msg = choice.message;
    // Append the assistant's reply to the message log; the LLM
    // expects this on the next call.
    messages.push({
      role: "assistant",
      content: msg.content || "",
      tool_calls: msg.tool_calls || undefined,
    });
    if (lastFinishReason === "stop") {
      finalContent = msg.content || "";
      break;
    }
    if (lastFinishReason !== "tool_calls" || !msg.tool_calls || msg.tool_calls.length === 0) {
      // Unexpected: LLM said "stop" but with no content, or
      // "length" or "content_filter" — treat as terminal.
      finalContent = msg.content || "";
      if (lastFinishReason === "length") {
        loopError = "model hit max token limit before completing";
      }
      break;
    }
    // Run each tool call sequentially. The OpenAI SDK returns
    // them in order; we preserve that order in the messages.
    for (const tc of msg.tool_calls) {
      const name = tc.function.name;
      let args = {};
      try {
        args = JSON.parse(tc.function.arguments || "{}");
      } catch (err) {
        const errResult = {
          ok: false,
          output: "",
          error: `tool args not valid JSON: ${err.message}`,
          duration_ms: 0,
          exit_code: 8,
        };
        tool_calls.push({ name, args: tc.function.arguments, result: errResult });
        messages.push({
          role: "tool",
          tool_call_id: tc.id,
          content: JSON.stringify({ ok: false, error: errResult.error }),
        });
        continue;
      }
      logger({ step, name, args });
      const result = await dispatchTool({
        name,
        args,
        ctx: {
          workdir: spec.workdir,
          allowlist_cmds: spec.allowlist_cmds,
          per_step_timeout_ms: spec.per_step_timeout_ms,
          max_output_bytes: spec.max_output_bytes,
        },
      });
      tool_calls.push({ name, args, result, duration_ms: result.duration_ms });
      // The LLM wants the tool result in a `tool` role message
      // with the matching `tool_call_id`. We serialize the
      // {ok, output, error, exit_code} so the LLM can read it.
      messages.push({
        role: "tool",
        tool_call_id: tc.id,
        content: JSON.stringify({
          ok: result.ok,
          output: result.output,
          error: result.error,
          exit_code: result.exit_code,
        }),
      });
    }
  }

  const finished_at = new Date().toISOString();
  if (finalContent === null && tool_calls.length >= spec.max_steps) {
    loopError = `exceeded max_steps=${spec.max_steps} without a final answer`;
  }
  return {
    job_id: spec.job_id,
    status: loopError ? "error" : "ok",
    output: finalContent || (tool_calls.length > 0 ? "" : null),
    tool_calls,
    usage: usage
      ? {
          input_tokens: usage.prompt_tokens || 0,
          output_tokens: usage.completion_tokens || 0,
          total_tokens: usage.total_tokens || 0,
        }
      : null,
    started_at,
    finished_at,
    finish_reason: lastFinishReason,
    error: loopError,
  };
}
