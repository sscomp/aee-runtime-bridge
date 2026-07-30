# AEE Executor Capability Discovery & Runtime Identity Enhancement — Implementation Report

Work order: `AEE/dispatch/TASK_AEE_EXECUTOR_CAPABILITY_ENHANCEMENT.md`
Type: Small Enhancement (Post-Implementation)
Priority: High
Repository: `/home/ubuntu/hermes-runtime-bridge`
Bridge HEAD: `07aefcb91fa11bd8dc6c8f4814ca3bc1fdb715d7`
Bridge branch: `master`
Generated at UTC: `2026-07-21T06:42:00Z`

---

## Execution Timing

- Start: 2026-07-21 (session)
- End: 2026-07-21 (same session)
- Wall-clock: single session, sequential implementation + verification.
- Real CLI smoke cost: one short Claude Code CLI invocation (tokens spent, bounded by `max_turns=10`, `timeout_sec=180`).

## Baseline

Before this enhancement the executor surface was:

- `POST /runs/executor` — dispatch to `claude-code-cli` or `hermes`; full evidence envelope.
- `gpt/aee_executor_openapi.json` — GPT-importable OpenAPI 3.0 schema for that one endpoint.
- `gpt/GPT_SETUP_GUIDE.md` — GPT wiring guide.
- Executor config: `config/executor.json` + `aee/runtimes/executor_config.py`.
- Envelope builder: `aee/runtimes/executor_api.py`.
- Evidence helpers: `aee/runtimes/executor_envelope.py`.
- CLI runner: `aee/runtimes/executor_cli.py`.
- Tests: `tests/test_executor_*.py`, `tests/test_openapi_executor_metadata.py`, `tests/smoke_executor_real_cli.py`.

Baseline test status (before changes): existing executor + openapi suites green (17 + 11 = 28 baseline run; broader router/router-evidence/claude-code suites also green).

## Files Changed

| File | Status | Purpose |
|------|--------|---------|
| `aee/runtimes/runtime_identity.py` | NEW | Factual runtime-identity collectors (Part B). |
| `aee/runtimes/executor_api.py` | MODIFIED | Added `runtime_identity` to `ExecutorRunResponse` + `build_executor_response()`. |
| `app.py` | MODIFIED | Added `GET /executors` endpoint (Part A); wired `runtime_identity` into all three `/runs/executor` response paths. |
| `gpt/aee_executor_openapi.json` | MODIFIED | Added `GET /executors`, `RuntimeIdentity` + `ExecutorsResponse` schemas, `runtime_identity` in `ExecutorRunResponse`, new examples. |
| `gpt/GPT_SETUP_GUIDE.md` | MODIFIED | Added "Capability discovery" section + renumbered; updated GPT natural-language instructions. |
| `tests/test_executor_capability_discovery.py` | NEW | Targeted tests (Parts A/B + OpenAPI validation). No existing test rewritten. |
| `AEE_EXECUTOR_CAPABILITY_ENHANCEMENT_VERIFICATION_SIDECAR.md` | NEW | Per-deliverable artifact verification sidecar. |
| `AEE_EXECUTOR_CAPABILITY_ENHANCEMENT_REPORT.md` | NEW | This report. |

No file outside the above set was edited. No existing test was rewritten.

## Architecture Impact

**Minimal and additive — by design.** The work order forbade any
Runtime / Executor / Adapter / Planner / Hermes / MiniMax / Claude CLI
redesign, refactor, or formatting cleanup. This enhancement touched
only the response envelope and a new read-only endpoint:

1. **New read-only path.** `GET /executors` is a pure FastAPI handler
   that reads `load_executor_config()` and returns
   `supported_executors` / `default_executor` / `aliases`. No dispatch,
   no task creation, no executor launch, no runtime mutation, no side
   effects. Auth unchanged (same `require_auth` bearer gate as every
   other endpoint).
2. **Envelope extension.** `runtime_identity` is a new *optional* field
   on `ExecutorRunResponse`, assembled by
   `aee.runtimes.runtime_identity.collect_runtime_identity()` and passed
   into the existing `build_executor_response()` builder. The builder
   signature gained one keyword-only parameter; all existing callers
   that omit it get `None` (no breakage).
3. **No routing change.** `executor_router.py`, `executor_config.py`
   canonicalisation, the MiniMax-free truthful routing, and the Claude
   Code CLI runner are untouched. `was_forced` is still always `false`
   on `/runs/executor`.

The new module `runtime_identity.py` is pure and read-only: it runs
read-only `git` (rev-parse / status / config) and `<binary> --version`
with short timeouts, returning `null` / `"unknown"` on any failure.
Never fabricates.

## Capability Discovery (Part A)

`GET /executors` returns:

```json
{
  "supported_executors": ["claude-code-cli", "hermes"],
  "default_executor": "claude-code-cli",
  "aliases": {
    "claude_code": "claude-code-cli",
    "claude-code": "claude-code-cli",
    "claudecode": "claude-code-cli"
  }
}
```

- `supported_executors` — straight from `config/executor.json`.
- `default_executor` — straight from config.
- `aliases` — config's `executor_aliases` with identity self-maps
  (`claude-code-cli -> claude-code-cli`) excluded, matching the work
  order's example shape. Every alias value is verified to point at a
  supported executor (asserted in tests).

Acceptance A–D satisfied.

## Runtime Identity (Part B)

The `runtime_identity` block is now present on every `/runs/executor`
response (claude-code-cli, hermes success, and hermes submit-failure).
Real CLI smoke captured this factual block:

```json
"runtime_identity": {
  "provider": "Claude Code",
  "provider_version": "2.1.216 (Claude Code)",
  "executor_binary": "/home/ubuntu/.local/bin/claude",
  "executor_version": "2.1.216 (Claude Code)",
  "runtime_bridge_version": "unknown",
  "bridge_commit": "07aefcb91fa11bd8dc6c8f4814ca3bc1fdb715d7",
  "bridge_branch": "master",
  "bridge_repository": "/home/ubuntu/hermes-runtime-bridge",
  "generated_at_utc": "2026-07-21T06:40:37Z"
}
```

Factual rules enforced (and tested):

- `provider` — display name from a fixed map (`Claude Code` / `Hermes`);
  `"unknown"` for an unrecognised executor (never a guess).
- `provider_version` / `executor_version` — from `<binary> --version`
  for `claude-code-cli`; `null` for `hermes` (no local binary).
- `executor_binary` — configured `claude_cli_binary` for
  `claude-code-cli`; `null` for `hermes`.
- `runtime_bridge_version` — shipped constant `RUNTIME_BRIDGE_VERSION`,
  currently `"unknown"` (no version stamp shipped; not fabricated).
- `bridge_commit` / `bridge_branch` — read-only `git rev-parse` on the
  bridge worktree; `null` when not a git worktree.
- `bridge_repository` — remote origin URL if configured, else the local
  bridge repo path (this bridge has no remote, so the local path is
  factual).
- `generated_at_utc` — real ISO-8601 UTC timestamp.

`bridge_commit` from the envelope equals `git rev-parse HEAD` of the
bridge repo (asserted in `test_runtime_identity_bridge_commit_matches_git`).
Acceptance E–G satisfied.

## OpenAPI

`gpt/aee_executor_openapi.json` updated:

- New path `GET /executors` (`operationId: aee_list_executors`,
  `x-openai-is-consequential: false`, `bearerAuth`).
- New schemas: `ExecutorsResponse`, `RuntimeIdentity`.
- `ExecutorRunResponse` now lists `runtime_identity` in `required` and
  `properties` (`$ref` to `RuntimeIdentity`).
- New/updated examples: `default_capabilities` for `/executors`;
  `runtime_identity` added to the `claude_code_cli_completed` example.
- `info.description` updated to mention discovery + `runtime_identity`.

Validation:

- `openapi_spec_validator.validate(doc)` → PASS (Acceptance H).
- `ExecutorsResponse` example validates against its schema (jsonschema
  Draft202012, after OpenAPI 3.0 `nullable` → `[T,"null"]` rewrite).
- `ExecutorRunResponse` example (with `runtime_identity`) validates
  against its schema → Acceptance L (GPT can import the JSON with no
  manual edits; both Actions callable).

## GPT Integration

`gpt/GPT_SETUP_GUIDE.md` gained a new "Capability discovery"
section documenting the recommended workflow:

```
GET /executors  →  select executor  →  POST /runs/executor
```

The natural-language GPT instructions (section 8) now tell the GPT to
call `aee_list_executors` first, pick from `supported_executors`
(canonicalising aliases), then call `aee_executor_run`, and to report
`runtime_identity` in its summary. Acceptance I satisfied.

## Tests

New file `tests/test_executor_capability_discovery.py` (15 tests), targeted only:

- `test_executors_returns_supported` / `_default` / `_aliases` — Part A.
- `test_executors_requires_auth` — auth unchanged.
- `test_executors_is_read_only` — only capability keys, stable across calls.
- `test_runtime_identity_exists_on_cli_envelope` — Part B presence.
- `test_runtime_identity_values_are_factual` — factual values, null/unknown rules.
- `test_runtime_identity_bridge_commit_matches_git` — Acceptance G.
- `test_runtime_identity_exists_on_hermes_envelope` — hermes async path.
- `test_runtime_identity_unknown_provider_for_unsupported` — never fabricate.
- `test_openapi_json_parses_and_validates` — Acceptance H.
- `test_openapi_has_executors_path_and_schema` / `_executor_response_includes_runtime_identity`.
- `test_openapi_executors_example_validates` / `test_openapi_executor_run_response_example_validates` — Acceptance L.

No existing test was rewritten.

## Regression

- New targeted suite: 15 passed.
- Existing executor + openapi suites (routing, unsupported, response
  contract, claude-code-cli, no-forced-minimax, timeout-cancel,
  artifact-evidence, openapi-metadata): 43 passed.
- Router / routing-evidence / claude-code-executor: 48 passed.
- Real CLI smoke (`AEE_REAL_CLI_SMOKE=1`): 1 passed — real evidence,
  factual `runtime_identity`, `MiniMax` absent from routing.

Acceptance J (Claude CLI smoke) and K (executor routing tests) satisfied.

## Artifact Verification

Every deliverable verified with `ls -la` / `wc -c` / `wc -l` /
`sha256sum`. Full block in
`AEE_EXECUTOR_CAPABILITY_ENHANCEMENT_VERIFICATION_SIDECAR.md`. Summary:

| File | bytes | lines | sha256 |
|------|------:|------:|--------|
| `aee/runtimes/runtime_identity.py` | 5936 | 155 | `f55c7a74…e1c2da` |
| `aee/runtimes/executor_api.py` | 5977 | 175 | `0a1af7bf…8d2f7c` |
| `app.py` | 70394 | 1697 | `f8bde529…da5f2` |
| `gpt/aee_executor_openapi.json` | 18258 | 355 | `d0d62436…6942a` |
| `gpt/GPT_SETUP_GUIDE.md` | 8489 | 234 | `dcf11611…04a67` |
| `tests/test_executor_capability_discovery.py` | 14563 | 366 | `a7cc0ade…7e1d` |

OpenAPI validation: PASS. Required-heading grep: all 9 guide sections
present, "Capability discovery" present.

## Git Evidence

Bridge worktree (read-only, no mutation):

- `HEAD` = `07aefcb91fa11bd8dc6c8f4814ca3bc1fdb715d7`
- branch = `master`
- remote = none (`bridge_repository` falls back to the local path, factual)
- Working tree: deliverables unstaged (`M app.py`; `??` for the new
  files and the `gpt/` directory). No commit, no stage.

The `/runs/executor` envelope's `git_evidence` is for the *target* repo
(`/home/ubuntu/Abacus`, HEAD `869a77f…`) and is independent of
`runtime_identity.bridge_commit` (the *bridge* repo HEAD `07aefcb…`).
Both are factual and correctly scoped.

## Production Safety

No commit, no push, no deploy, no restart, no merge, no rebase, no
stash, no delete, no move, no stage-all. Only unstaged working-tree
edits. The running bridge process was not restarted; all verification
used the in-process FastAPI `TestClient` plus one real CLI smoke run.

## Remaining Risks

- **Per-request subprocess cost.** `collect_runtime_identity` runs up to
  three `git` calls (≤5s each) and one `<binary> --version` (≤8s) per
  `/runs/executor` response. In practice each completes in tens of ms
  (local git, local binary). If a future binary's `--version` blocks,
  the timeout caps it and the field degrades to `null` (no hang).
- **`runtime_bridge_version` is `"unknown"`.** Factual today (no
  version stamp shipped). When a version is stamped, set
  `RUNTIME_BRIDGE_VERSION` in `runtime_identity.py` in one place.
- **No remote URL.** `bridge_repository` is the local path. Factual;
  if a remote is added later, `bridge_repository` automatically reports
  the origin URL (no code change).
- **OpenAPI 3.0 `nullable`.** The example validators rewrite
  `nullable: true` to `[T, "null"]` for Draft202012. This is a test-only
  transform; the shipped spec stays standard OpenAPI 3.0.3 (GPT-importable).
- **Aliases surface.** Returns all non-identity aliases from config
  (`claude_code`, `claude-code`, `claudecode`). The work-order example
  shows a subset; the implementation returns the full, correct set.

## Review Ready

Yes. All acceptance criteria A–L met and verified:

- A. `GET /executors` exists.
- B. `supported_executors` correct.
- C. `default_executor` correct.
- D. `aliases` correct (non-identity, all map to supported).
- E. `runtime_identity` exists.
- F. `provider_version` factual (`2.1.216 (Claude Code)`).
- G. `bridge_commit` factual (matches `git rev-parse HEAD`).
- H. OpenAPI validates.
- I. GPT setup guide updated.
- J. Existing Claude CLI smoke still passes.
- K. Existing executor routing tests still pass.
- L. `gpt/aee_executor_openapi.json` imports with no manual edits;
  both Actions validatable against their schemas.

## Commit Ready

**No** — per the work order's Production Safety section, no commit /
push / stage is performed by this task. The working tree is left with
unstaged edits ready for a human (or a follow-up atomic-commit task) to
review and commit.

## Telegram

Completion notification sent via `dongxinmeowm2bot`
(token from `/home/ubuntu/.hermes/.env`) to chat `5132341473`:
`telegram ok: True, message_id: 7686`.

## Final Verdict

**PASS.** All twelve acceptance criteria (A–L) are satisfied and
verified with real evidence. No regression. Production safety
respected (no commit/push/deploy/restart). OpenAPI validates and is
GPT-importable. Telegram attempted and succeeded.