# Runtime capability matrix

Derived from real evidence only. Contract/code rows cite the reviewed source
snapshot at commit `2b123e21` (merge of PR #7 `os-native-profile-integration`;
verified ancestor `67d2c352`) — inspected 2026-10-20 from the host-local
release tree. Live-host runtime rows cite operator acceptance receipts and
job IDs; none was re-executed while documenting and none is claimed as
fresh revalidation. Where receipts differ from a repo pin, both are stated.

## Evidence legend

| Label | Meaning |
|---|---|
| **Confirmed** | Independently inspected in source/contract this cycle |
| **Reported** | Operator receipt / job record / install evidence; not re-executed in this cycle |
| **Not Tested** | Open gate — no acceptance evidence exists or is cited |

## Adapter registry semantics (Confirmed — `aee/mcp_runtime/job_adapters.py` @ 2b123e21)

| Adapter | `read_only` enforcement | `write` enforcement | `autonomous` | Notes |
|---|---|---|---|---|
| codex | `NATIVE_SANDBOX` — `codex exec --sandbox read-only` | `NATIVE_SANDBOX` — `workspace-write`, job dir + /tmp, network off by default | Mapped + capability-probed | Identity (path+SHA-256) re-checked at dispatch; sandbox pin: vendored Bubblewrap 0.11.2 from openai/codex `rust-v0.159.2` |
| claude | `TOOL_ALLOWLIST` — restricted safe mode, only Read/Glob/Grep, empty strict MCP | `NATIVE_SANDBOX` — headless `acceptEdits` (interactive-only tools denied) | Mapped + capability-probed | Plan mode alone is not a read-only boundary (stated in code) |
| pi | `TOOL_DENYLIST` — `--tools read` only, no MCP/extensions | **`UNSUPPORTED`** — refused, never simulated | Mapped; registry does not pre-refuse | `--help` capability probe required before dispatch |
| hermes | **`UNSUPPORTED`** — refused even though installed | **`UNSUPPORTED`** | Registry mapping exists; policy admits only qualified agents | Refusal is by design: non-interactive toolset restriction not verifiable on the host |

Resolution contract: absolute override env `A3_ADAPTER_<NAME>_BIN` first
(relative/missing/non-executable ⇒ fail closed), else `PATH` under the
service identity's effective PATH; failure codes
`ADAPTER_NOT_INSTALLED` / `ADAPTER_UNSUPPORTED`; unknown agents
`INVALID_AGENT`. `scripts/audit_adapters.py` reports resolution facts
under the intended service identity.

**Registry support does NOT prove an E2E job succeeded in that mode.**
Live dispatchability below is always per host and dated.

## A3 (omarchy-a3, os-native profile, installed from `2b123e21` on 2026-10-20)

Install verdict: **PASS WITH CONDITIONS** (operator evidence bundle
`~/AEE_A3_REINSTALL_EVIDENCE_20261020/`, 0700, zip + SHA-256). Conditions
include the open gates listed below. Codex provenance note: the repo
sandbox-profile record pins the reviewer chain at Codex `0.159.2`; the
running A3 host resolves native Codex `0.160.0` (operator-repinned
`A3_CODEX_BIN`/`A3_ADAPTER_CODEX_BIN` to the vendored native ELF after the
npm JS wrapper failed native-ELF identity checks).

| Capability | Status | Evidence (source / date / ID) |
|---|---|---|
| Codex `read_only` E2E dispatch | **Reported** — completed exit 0, `NATIVE_SANDBOX` | Read-only work order receipt: job `A3JOB-20261019-928c4174f778e8f7edba4049e2b7be28` — 2 native tool events started/completed, 0 failed. Reinstall-order receipt: job `A3JOB-20261019-5e7862f0…` queued→running→completed, exit 0, exec via native codex 0.160.0 (sha recorded). Adjacent codex repin E2E `A3JOB-20261019-8a218391…` completed exit 0. All 2026-10-19/20. |
| Claude Code `read_only` E2E dispatch | **Reported** — completed exit 0, `TOOL_ALLOWLIST` | Work order: job `A3JOB-20261019-6929c5831f9b3c8937cd6a0ee06ee6b9` completed exit 0 (Claude Code 2.1.295). Reinstall-order receipt: job `A3JOB-20261019-53c1e559…` restricted safe mode Read/Glob/Grep + empty strict MCP config. Claude auth on this host requires `ANTHROPIC_*` env + user dbus (install receipt conditions). |
| Codex `write` (trusted dispatch) | **Reported** — real write job created a proof file under trusted policy | Install verification record, 2026-10-20. |
| Claude `write` | **Not Tested** as E2E (registry `write_enforcement` = NATIVE_SANDBOX) | Registry support confirmed in code only. |
| Codex / Claude `autonomous` | **Not Tested** as dedicated E2E | Policy `write-autonomous` enabled and verified by a write-mode job; no separate autonomous receipt cited. |
| Pi dispatch | **Not Tested**; registry `read_only` only, `write` unsupported | Adapter availability Reported — installed (audit resolves absolute bin); autonomous dispatch disabled per operator record 2026-10-20; no Pi E2E receipt cited. |
| Hermes dispatch | **Fail-closed by design** (Confirmed refusal path) | Registry `read_only` = UNSUPPORTED; host verification Reported — hermes refused both modes, 2026-10-20. |
| Six-tool surface + 401 negatives | **Reported** — PASS | A3 local/tunnel gates G1–G8, 2026-10-20 receipts. |
| Trusted-dispatch DAC boundary | **Reported** — `/root` dispatch refused `INVALID_WORKING_DIRECTORY` | Install verification, 2026-10-20. |
| Restart persistence + fresh job-store continuity | **Reported** — PASS | A3 gates, 2026-10-20. |
| ChatGPT-originated External Plugin E2E | **Not Tested** — open gate | Receipts 2026-10-20; plugin may cache a rotated bearer (refresh flow). |
| Cold boot (linger + reboot cycle) | **Not Tested** — open gate; reboot not yet authorized | `loginctl enable-linger` is required for os-native units to survive reboot; installer reports lingering state and never enables it. |

## A2 host (restricted Secure MCP deployment)

| Capability | Status | Evidence (source / date) |
|---|---|---|
| Real jobs across Claude / Codex / Hermes | **Reported (historical)** — prior parity audit reports real jobs; distinguish from fresh revalidation | Prior A2 parity audit record; not revalidated in this cycle. |
| Trusted dispatch chain (ChatGPT connector → tunnel → bearer → policy principal) | **Confirmed** contract (`docs/mcp-runtime-autonomous.md` @ 2b123e21); host gate status follows that deployment's receipts | Policy checked 4× per job; fail-closed on malformed/revoked policy. |
| ChatGPT-originated external E2E | **Not Tested** — final external ChatGPT E2E must be performed independently; local MCP tests do not satisfy that gate | Stated in the trusted-dispatch contract itself. |

## Known limitations across hosts (Confirmed in contract)

- **No multi-tenant auth**: every caller holding the gateway bearer shares
  its full authority; per-user job ownership is not implemented.
- **One active job** per gateway (`BUSY` on second admission); no queue, no
  automatic retry of interrupted jobs.
- **Redaction is best effort**, not a data-classification system; secrets and
  sensitive source may still pass through permitted task output.
- **Static availability ≠ dispatchability**: registry declares agent
  install/supported modes statically; native-ELF/probe checks at dispatch can
  still refuse (the A3 codex npm-wrapper incident).
- Bounded I/O everywhere: 65,536-byte stdout/stderr/result caps, 4,000-char
  summary, 2,000-char log, 8,000-char task, snapshot bounds 10,000 files /
  4 MiB per file / 64 MiB total; Codex timeout ≤ 900 s.
- **Not production-readiness claims**: ChatGPT-originated Plugin E2E and
  cold boot remain open; `write`/`autonomous` remain policy-gated per
  principal and per host. Never claim unrestricted write access from any of
  the above — os-native write authority is exactly the service OS identity's
  DAC/ACL scope plus the adapter's native write confinement.