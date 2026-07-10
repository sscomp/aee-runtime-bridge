// pi-agent/runtime/lib/provider.js
// OpenAI-compatible LLM client. Reads base URL + API key + model
// from constructor args (which the CLI populates from --provider-*
// flags or PI_PROVIDER_* env vars).
//
// The OpenAI SDK (npm:openai) supports any OpenAI-compatible
// endpoint by setting `baseURL` + `apiKey`, so this works for
// OpenAI, OpenRouter, Anthropic-via-OpenAI-proxy, Ollama,
// vLLM, etc. The daemon is responsible for picking a compatible
// base URL; the runtime doesn't care.
//
// On provider error, throws an `OpenAIError`. The CLI catches
// it and exits with code 3.

import OpenAI from "openai";

export function makeProvider({ base_url, api_key, model }) {
  if (!base_url) {
    throw new Error("provider base_url is required (--provider-base-url or PI_PROVIDER_BASE_URL)");
  }
  if (!api_key) {
    throw new Error("provider api_key is required (--provider-api-key or PI_PROVIDER_API_KEY)");
  }
  if (!model) {
    throw new Error("provider model is required (--provider-model or PI_PROVIDER_MODEL)");
  }
  const client = new OpenAI({ baseURL: base_url, apiKey: api_key });
  return { client, model };
}

// One provider call. The OpenAI SDK is async; we wrap it for
// testability. Returns the raw `chat.completions.create` response.
export async function callProvider({ client, model, messages, tools }) {
  return await client.chat.completions.create({
    model,
    messages,
    tools,
    // We do not set tool_choice; the LLM decides when to use
    // a tool vs return a final answer.
  });
}
