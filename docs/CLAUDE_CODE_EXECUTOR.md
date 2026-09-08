# Claude Code Executor — External Anthropic-Compatible Providers

Operational reference for running the AEE Bridge's `claude-code-cli`
executor (`POST /runs/executor`) against an external Anthropic-compatible
provider, with **Ollama Cloud** as the concrete example. It consolidates
the verified lessons from the 2026-09-08 investigation chain
(TASK-20260908-0026 audit, -0027 protocol audit, -0028 runtime fix; see
[Provenance](#provenance)).

Audience: operators of this bridge. No secrets appear in this document —
every credential value is a placeholder.

## Architecture

```
Caller (authorized bearer key)
        │  POST /runs/executor {executor: "claude-code-cli", prompt, repo_path, ...}
        ▼
AEE Runtime Bridge  (FastAPI, 127.0.0.1:8787, user service aee-bridge.service)
        │  routes via config/executor.json + AEE_EXECUTOR_* env overrides
        │  (aee/runtimes/executor_config.py)
        ▼
ClaudeCodeCliRunner  (aee/runtimes/executor_cli.py)
        │  builds child env: mirror ANTHROPIC_AUTH_TOKEN → ANTHROPIC_API_KEY
        │  (when the latter is unset), then allow-list filter
        │  (aee/adapters/claude_code_provider.py::_filter_env)
        ▼
claude-code-cli subprocess  ("claude -p", no shell, own process group)
        │  authenticates with the forwarded env vars
        ▼
Anthropic-compatible provider  (api.anthropic.com, Ollama Cloud, or any
   endpoint speaking the /v1/messages protocol at ANTHROPIC_BASE_URL)
```

Key properties (all verified in code):

- The subprocess is spawned with an argv list, never a shell; the prompt is
  a positional argument (`aee/adapters/claude_code_provider.py`).
- The child env is an explicit allow-list intersection, never a wholesale
  `os.environ` copy. Bridge secrets such as `BRIDGE_API_KEY` never reach
  the worker.
- Credential routing is provider-agnostic: anything that speaks the
  Anthropic Messages protocol works if `ANTHROPIC_BASE_URL` +
  `ANTHROPIC_AUTH_TOKEN` point at it.
- `--bare` is OFF by default; bare/hermetic mode can break env-based auth
  (documented in `claude_code_provider.py`, AEE-6.3 case study).

## Why interactive shell exports are not enough

The most common failure mode for this executor is not the provider — it is
the environment delivery between the operator's shell and the service.

A systemd-style service (including this repo's user unit
`~/.config/systemd/user/aee-bridge.service`, which reads
`EnvironmentFile=%h/aee-runtime-bridge/.env`) does **not** run a login or
interactive shell. Variables exported in `~/.bashrc`, `~/.profile`, or
`~/.zshrc` are invisible to it.

The generic `~/.bashrc` early-return pitfall: most distributions ship a
guard near the top of the file, e.g.

```bash
case $- in
    *i*) ;;
      *) return;;
esac
```

(or the equivalent `[[ $- != *i* ]] && return`). Every `export` placed
**after** that line only executes in interactive shells. `bash -c` (a
non-interactive shell — the shape a service or test harness effectively
uses) evaluates the guard, returns early, and never reaches the exports.
The result is a classic split-brain: `bash -ic 'echo $VAR'` shows the
variable SET, `bash -c 'echo $VAR'` and the service show it UNSET. This is
exactly what the TASK-0026 audit reproduced on this host: all nine relevant
variables were UNSET in non-interactive contexts and SET only interactively,
while the bridge service environment had zero `ANTHROPIC_*` names.

Generic fix (applies to any service, not just this one): put the service's
required variables in a **dedicated environment file the service manager
loads**, not in shell startup files. For this bridge that is
`~/aee-runtime-bridge/.env` (mode `0600`, gitignored — see
[Safe secret storage](#safe-secret-storage)), read once at uvicorn startup
by `load_dotenv()` and by the unit's `EnvironmentFile=`.

## Environment variables

### Provider connection (required for external providers)

| Variable | Purpose | Notes |
|---|---|---|
| `ANTHROPIC_AUTH_TOKEN` | Bearer credential for the provider | The auth var Claude Code reads for token-based auth. This repo's runner automatically mirrors it to `ANTHROPIC_API_KEY` for the worker when the latter is unset (`aee/runtimes/executor_cli.py::_build_claude_env_mirror`) — one value covers both code paths. |
| `ANTHROPIC_BASE_URL` | Anthropic-compatible endpoint base URL | Without it the CLI targets the public Anthropic API and a third-party token gets HTTP 401. Set it whenever the token is not a real Anthropic key. |
| `ANTHROPIC_MODEL` | Default model id sent to the provider | Canonical per-session model override; also forwarded by every allow-list in this repo. |

### Default-model mapping (recommended with custom providers)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Model id substituted when Claude Code resolves its `sonnet` alias |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Same for `opus` |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Same for `haiku` |

Mapping all three avoids hard failures when a task requests an alias your
provider does not serve under that name (proven live during TASK-0028:
unmapped alias → provider model rejection; mapped trio → exit 0).

### Context budget

| Variable | Purpose |
|---|---|
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | Context window budget; raises the auto-compact ceiling above the 200k default when your provider supports more (per the in-repo runner comment at `aee/adapters/claude_code_provider.py:139-143`; CLI-anchored behavior). Present in the executor-path (`claude_code_provider.py`) and `claude_cli.py` adapter allow-lists — **not** `claude_code_executor.py` (the executor path forwards it; forgetting it once caused TASK-20260824-0039). |

### Catalog override (verify before use)

| Variable | Purpose |
|---|---|
| `CLAUDE_CODE_MODEL_CATALOG` (and `CLAUDE_CODE_MODEL_CATALOG_URL`) | Catalog-override mechanisms that exist in the installed Claude Code bundle for describing custom model ids. **This repository does not use or validate any syntax for it.** The exact value format was never proven on this host (TASK-0028 deliberately skipped it), so this doc intentionally does not document one. Consult `claude --help` / the vendor docs for your installed version before setting it. |

### Executor configuration of this bridge (for completeness)

These configure the bridge's executor layer itself (parsed by
`aee/runtimes/executor_config.py`, documented in `.env.example`):
`AEE_CLAUDE_CLI_BINARY`, `AEE_EXECUTOR_DEFAULT`,
`AEE_EXECUTOR_DEFAULT_TIMEOUT`, `AEE_EXECUTOR_MAX_TURNS`,
`AEE_EXECUTOR_DEFAULT_CWD`, `AEE_EXECUTOR_REPO_ALLOWLIST`.

### How the env reaches the worker (the pipeline that must not break)

1. `.env` → bridge service process environment (at startup, via
   `EnvironmentFile=` + `load_dotenv()`).
2. Bridge → runner: `ClaudeCodeCliRunner.run()` applies the auth mirror
   (`ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY` when unset) on top of the
   parent environment.
3. Runner → `claude` subprocess: `_filter_env` keeps only names in
   `_ALLOWED_ENV_VARS` (`aee/adapters/claude_code_provider.py`), which
   includes `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`,
   `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`,
   `ANTHROPIC_DEFAULT_SONNET_MODEL`/`_OPUS_`/`_HAIKU_`,
   `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, plus infra vars (`PATH`, `HOME`, …).

Two caveats worth knowing before editing code or env:

- Allow-list matching is **exact-name, case-sensitive**. Note that
  `_ALLOWED_ENV_VARS` contains a mixed-case entry
  `ANTHROPIC_DEFAULT_Sonnet_MODEL` (historical) while
  `aee/adapters/claude_cli.py` uses the all-caps spelling. Always use the
  exact all-caps names shown in the tables above.
- There are several allow-lists in the tree (executor path
  `claude_code_provider.py`; adapter paths `claude_cli.py`,
  `claude_code_executor.py`). They cover the same core names; if a future
  variable needs forwarding on all paths, add it to each list.

## Safe secret storage

- Store provider credentials in `~/aee-runtime-bridge/.env` with mode
  `0600`. The file is gitignored (`.gitignore` lines 13–15:
  `.env`, `.env.*`, `!.env.example`) and must stay untracked.
- Never commit secrets — including "examples that happen to be real".
  `.env.example` carries variable **names and placeholders only**.
- Do not duplicate a provider token under multiple names. On this
  integration the single `ANTHROPIC_AUTH_TOKEN` value suffices; the code
  derives `ANTHROPIC_API_KEY`, and `OLLAMA_API_KEY` is deliberately not
  persisted (the CLI never reads it).
- If a credential appeared in chat, logs, screenshots, or any output
  channel, treat it as compromised and **rotate it** — then update the env
  file and restart the service (a separate, user-owned action).
- Do not paste tokens into shell commands (argv ends up in process listings
  and shell history). Edit the env file with an editor instead.

## Ollama Cloud example (placeholders only)

Ollama Cloud exposes an Anthropic-compatible endpoint at `https://ollama.com`
and serves cloud model ids such as `<provider-model-id>:cloud`. Add to
`~/aee-runtime-bridge/.env` (values here are placeholders — substitute your
own):

```bash
# --- Claude Code executor → Ollama Cloud (Anthropic-compatible) ---
ANTHROPIC_AUTH_TOKEN=<ollama-api-key>
ANTHROPIC_BASE_URL=https://ollama.com
ANTHROPIC_MODEL=<model-id>:cloud
ANTHROPIC_DEFAULT_SONNET_MODEL=<model-id>:cloud
ANTHROPIC_DEFAULT_OPUS_MODEL=<other-model-id>:cloud
ANTHROPIC_DEFAULT_HAIKU_MODEL=<model-id>:cloud
CLAUDE_CODE_MAX_CONTEXT_TOKENS=200000
```

Then **restart the bridge** (`.env` is read once at startup):

```bash
systemctl --user restart aee-bridge.service
```

Operational notes from this host's runs:

- `https://ollama.com` works with Claude Code's own request shape
  (`POST /v1/messages?beta=true`, streaming SSE) — verified end-to-end in
  TASK-0027; the earlier "protocol incompatibility" hypothesis was
  disproved. It remains an undocumented upstream surface and may change
  without notice.
- The credential to use is the same value your interactive setup uses for
  Ollama Cloud. Do not persist it twice; see
  [Safe secret storage](#safe-secret-storage).

## Custom model catalog warning (non-fatal)

Model ids that are not in Claude Code's built-in catalog — for example
`glm-5.3-flash:cloud` on CLI 2.1.263 — produce a stderr notice similar to:

```
[claude-code:unrecognized_model] {"model":"<model-id>:cloud","query_source":"sdk"}
```

and a catalog hint mentioning `behavesAs` / `modelPicker` /
`modelOverrides` / `CLAUDE_CODE_MODEL_CATALOG`.

Facts established on this host (do not generalize beyond them):

- The warning is **non-fatal** on 2.1.263: both TASK-0028 smoke runs exited
  0 with the warning present, and the probe matrix in TASK-0026 showed the
  model message and the auth failure are independent (switching to an
  in-catalog `--model sonnet` removed the warning but still died on the
  missing credential; supplying credentials made auth succeed with the
  warning still present).
- The warning's own text advertises mapping mechanisms (`behavesAs`,
  `modelOverrides`, `CLAUDE_CODE_MODEL_CATALOG`). These exist, but the
  exact supported syntax was never proven here, so this repo neither
  configures nor documents a specific syntax. Treating the warning as
  accepted noise is a valid operating posture.
- A related escape hatch referenced in code comments
  (`aee/adapters/claude_cli.py`) is
  `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1`, forwarded by
  the adapter layer when the operator exports it. Its semantics are
  version-dependent; verify against your installed CLI before relying on
  it.

## Verification procedure

Run these after any env change (all commands are secret-safe: they reveal
variable NAMES / SET-UNSET status only, never values).

1. Service env presence (names only):

   ```bash
   PID=$(systemctl --user show -p MainPID --value aee-bridge.service)
   tr '\0' '\n' < /proc/$PID/environ | cut -d= -f1 \
     | grep -E '^(ANTHROPIC_|CLAUDE_CODE_)' | sort
   # Expect: ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL, ANTHROPIC_MODEL,
   # ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU}_MODEL, CLAUDE_CODE_MAX_CONTEXT_TOKENS — all SET
   ```

2. AEE health:

   ```bash
   curl -sS http://127.0.0.1:8787/health | python3 -m json.tool
   # Expect: "status": "ok" and Hermes reachable
   ```

3. Executor smoke on the same path that fails when env is broken
   (bearer = one of your bridge client keys):

   ```bash
   curl -sS -X POST http://127.0.0.1:8787/runs/executor \
     -H "Authorization: Bearer $BRIDGE_CLIENT_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"executor":"claude-code-cli",
          "prompt":"Reply with exactly this token and nothing else: CLAUDE-OLLAMA-OK",
          "timeout_sec":240,"repo_path":"/tmp"}'
   ```

   Success signature: HTTP 200, `exit_code: 0`,
   `runtime_identity.executor_binary` = your `claude` path,
   `stdout_summary` = `CLAUDE-OLLAMA-OK`. stderr may still contain the
   non-fatal catalog warning (see above).

## Failure signature triage

| Signature | Meaning | Distinguishing evidence |
|---|---|---|
| `Not logged in · Please run /login`, exit 1, immediate | The worker subprocess had **no usable credential** | `ANTHROPIC_AUTH_TOKEN` UNSET in the service env (check #1); env present interactively only (bashrc early-return split) |
| `API Error: 401 Unauthorized` (after a long hang / retries) | Endpoint reachable; **credential value rejected** by the provider | All names SET (check #1 passes); value stale/revoked/wrong; same token rejected on direct provider probes |
| `[claude-code:unrecognized_model] …` in stderr, run **succeeds** | Model id outside the CLI's built-in catalog; **cosmetic, non-fatal** | exit 0 + expected stdout despite the warning |
| Connection refused / timeout / TLS errors, no HTTP status | **Network or endpoint-layer** problem (base URL typo, DNS, egress) | No provider HTTP status anywhere; `ANTHROPIC_BASE_URL` wrong or unreachable |

The `Not logged in` vs `401` distinction matters: TASK-0027 proved a 401
was a stale-credential symptom, not a protocol defect — the endpoint
accepted Claude Code's exact request shape once a valid credential was
supplied.

## Troubleshooting matrix

| Symptom | Likely cause | Safe check (reveals nothing secret) | Fix direction |
|---|---|---|---|
| Smoke: `Not logged in`, exit 1 | Env never reached the service (bashrc-only exports; `.env` missing the names) | Check #1 — names in `/proc/$PID/environ`; compare `bash -ic` vs service env | Put the names in `~/aee-runtime-bridge/.env`; restart the bridge |
| Smoke: 401 Unauthorized | Credential value rejected (stale/revoked/mistyped) | Check #1 passes but provider probe rejects the value | Rotate/reissue the credential; update `.env`; restart |
| Catalog warning in stderr; run completes | Custom model id not in CLI catalog | Run completes with exit 0 | Accept as non-fatal, or map the model via vendor-documented mechanisms (verify syntax first) |
| 401 with correct-looking token on a brand-new endpoint | `ANTHROPIC_BASE_URL` unset → token sent to public Anthropic API | Check #1: `ANTHROPIC_BASE_URL` SET? | Set `ANTHROPIC_BASE_URL` to the provider; restart |
| "unknown model" hard failure / empty stdout, exit 1 (older hosts) | Unknown-model window enforcement refusing the request (version-dependent) | CLI version; stderr content | Map the model, or evaluate `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1` after verifying semantics for your version |
| `ProviderError` / spawn failure | Binary missing at `claude_cli_binary` path | `ls -la <binary>`; `claude --version` | Fix `AEE_CLAUDE_CLI_BINARY` / `config/executor.json::claude_cli_binary` |
| Run hangs then `timeout` | Provider slow/unreachable; or model generates endlessly | Same request interactively; endpoint reachability | Check network/endpoint; review `timeout_sec` |
| Smoke works in `bash -c` manually but not via bridge | Test ran with the *operator's* env, not the service's | Re-check #1 against the service PID, not your shell | Fix the service env source (`.env`), not the shell |

## Restart semantics

Changing runtime env (`.env`, unit files, service overrides) requires an
**AEE Bridge restart** to take effect — the service reads its environment
once at startup. This documentation change itself performs no restart and
requires none.

## Provenance

- TASK-20260908-0026 — read-only runtime-environment audit (root causes
  RC1 env-delivery break, RC2 catalog warning mechanism, evidence matrix):
  `data/artifacts/a2_claude_ollama_runtime_environment_audit_20260908.md`
- TASK-20260908-0027 — 401 protocol-compatibility audit ( disproved the
  protocol-incompatibility hypothesis; endpoint accepts Claude Code's
  request shape with a valid credential):
  `data/artifacts/a2_claude_ollama_401_protocol_compatibility_audit_20260908.md`
- TASK-20260908-0028 — authorized runtime fix (`.env` names + one
  restart + same-path smoke `CLAUDE-OLLAMA-OK`):
  `data/artifacts/a2_claude_ollama_runtime_full_fix_20260908.md`
- TASK-20260908-0031 — repository assessment / documentation dispatch
  plan (this document is its implementation).

Code references: `aee/runtimes/executor_cli.py` (runner + auth mirror),
`aee/adapters/claude_code_provider.py` (subprocess provider + env
allow-list), `aee/adapters/claude_cli.py` (adapter allow-list + operator
notes), `aee/runtimes/executor_config.py` (executor env parsing),
`config/executor.json`, `.env.example`, `.gitignore`.