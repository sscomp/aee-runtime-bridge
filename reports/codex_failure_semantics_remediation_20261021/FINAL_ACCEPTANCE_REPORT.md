# FINAL_ACCEPTANCE_REPORT — Codex failure-semantics remediation & acceptance reconciliation

Date: 2026-10-21 (host local; UTC timestamps inside artifacts).
Tracked finalization of the [original acceptance report](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/FINAL_ACCEPTANCE_REPORT.md)
(SHA256 `de070e61249c4c91f611131dfd875e169549770b7ce60f61b46f3be45124f940`).
Historical evidence remains in the host-local evidence directory linked below;
those absolute links require access to that host. The original bundle predates
the ChatGPT result recorded in [the bounded live receipt](LIVE_E2E_RESULTS.md).
That receipt records the operator-provided verified result; this documentation
finalization did not independently re-fetch the job.

This is a documentation-only acceptance update. No runtime changes, deployment,
service restart or reboot were performed during finalization.

## Verdict: PASS WITH CONDITIONS

Core fix, deployment, gateway E2E and service restart persistence have prior
verification. The ChatGPT positive read-only E2E gate now passes within the
scope below. **Reboot persistence remains OPEN. This is not full signoff.**

## Commit / PR / merge summary

| Item | Value |
|---|---|
| Remediation commit | `42ccb86e558956f3370bd29e6bf6fe5a396524a6` (branch `fix/codex-tool-failure-semantics`, on top of reviewed `31152cc1…`) |
| PR | [#9](https://github.com/sscomp/aee-runtime-bridge/pull/9), 2 commits / 8 files in the full PR |
| Merge SHA (main) | `d5719e7df21cbae5d2ce8b9f5f0fa213c542623b`, PR #9 MERGED 2026-10-20T17:18:01Z, post-merge CI success |
| Remediation commit files changed (not the whole two-commit PR) | 4 files, +244/−15 (`git diff --stat` in `logs/remediation_patch.diff`): `aee/mcp_runtime/{event_stream,executor,process}.py`, `tests/mcp/test_codex_tool_failure_recovery_semantics.py` |

## F01–F06 disposition matrix

| Finding | Disposition | Evidence |
|---|---|---|
| F01 negative-control contradiction | **CLOSED** — v1 report claim corrected (actual: earlier same-turn message is emitted on both paths; final-answer rule now exact, fail-closed on blank/textless trail); semantics retained, no contract change on real streams | [BEFORE_AFTER_RECEIPTS.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/BEFORE_AFTER_RECEIPTS.md) §1, [SEMANTICS_CONTRACT.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/SEMANTICS_CONTRACT.md) v2, tests P04–P07 |
| F02 TypeError on `exit_code=[]`/`{}` | **CLOSED (code fix)** — de-hashed counting; malformed values → sticky `REQUIRED_TOOL_FAILED` on both paths, 9-value table test incl. integer matrix | tests P02/P03, `REPRO_PRE/POST/DEPLOYED.json` |
| F03 streamed/buffered precedence; parser-before-exit vs contract | **CLOSED (code fix)** — `run_streamed` EOF now maps process exit before parser verdict (buffered parity); sticky fatal still wins exit 0; mid-stream parse raises keep their position (documented) | tests E01–E04, [SEMANTICS_CONTRACT.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/SEMANTICS_CONTRACT.md) v2 precedence |
| F04 obsolete `98bde9b` ref + baseline typo | **CLOSED** — corrected identity, path, and rollback doc naming the real deployed pre-cutover release | [ROOT_CAUSE_AND_SCOPE.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/ROOT_CAUSE_AND_SCOPE.md), [ROLLBACK_PLAN.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/ROLLBACK_PLAN.md) |
| F05 test count/log inconsistencies | **CLOSED** — actual totals collected with raw logs: mcp 388 (383 pass/5 labeled skips), matrix file 33, profiles 62, aee.tests 66; pre-patch actual file count 22 (v1 claimed 21) | [PARITY_TEST_MATRIX.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/PARITY_TEST_MATRIX.md), [TEST_RESULTS.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/TEST_RESULTS.md), `logs/` |
| F06 process identity / live E2E / restart persistence | **CLOSED (restart-level)** — deployment identity proven by `/proc/<pid>/cmdline` + `<pid>/exe` + health pid match on release `d5719e7…`; live E2E positive/recovery/three negatives all match; restart persistence verified (**reboot gate remains OPEN**, below) | [DEPLOYMENT_RECEIPT.json](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/DEPLOYMENT_RECEIPT.json), [historical gateway E2E results](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/LIVE_E2E_RESULTS.md), [RESTART_PERSISTENCE.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/RESTART_PERSISTENCE.md) |

## Prior tests and raw evidence

- Suites + totals: [TEST_RESULTS.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/TEST_RESULTS.md) (raw logs + exit codes in `logs/`); CI
  equivalent reproduced locally and again green on GitHub for PR #9 (8/8) and
  post-merge main.
- Focused parity/precedence: matrix classes + `StreamedEofPrecedence`
  (real subprocesses), file total 33 tests.
- Native E2E (pre-deploy, disposable read-only sandbox): raw stream
  `native_e2e_stream.jsonl` (sha256 `dbe4b757…4aa8b`), process exit 0
  independent, both decision paths replay `completed` with `tools_failed=1`
  ([NATIVE_E2E_LOCAL.json](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/NATIVE_E2E_LOCAL.json)).

## Deployment / PID / E2E / restart proof (one line each)

- Deployed release: `/home/sscomp/aee-releases/d5719e7df21cbae5d2ce8b9f5f0fa213c542623b`
  (packaging build+verify PASS from the accepted merge commit; module hashes
  equal the reviewed patch tree).
- Process identity: pre-cutover PID 1982341 → cutover PID 2935422 → restart
  PID 2950124; every step's `/proc/<pid>/cmdline` names the release served;
  `/health` pid == MainPID at each observation ([DEPLOYMENT_RECEIPT.json](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/DEPLOYMENT_RECEIPT.json),
  [RESTART_PERSISTENCE.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/RESTART_PERSISTENCE.md)).
- E2E job IDs: positive `…3b954077a32c…`, recovery `…e393b8e6b2d0…`
  (`tool_event_record.tools_failed=1`), restart smoke `…3354ee81e76c…`;
  negatives dispatch-refused (`INVALID_WORKING_DIRECTORY`, `INVALID_TASK`,
  `ADAPTER_UNSUPPORTED`).

## ChatGPT live read-only E2E acceptance update

Verified job `A3JOB-20261020-02403a2cb33e2efb93615351a941fbfb` completed:
`exit_code=0`, `error_code=null`, 5 tools started / 5 completed / 0 failed,
15 native events with `turn.completed`; stdout 5410 bytes, no truncation;
result retrieved. Full fields, release source path and all three module hashes
are in [LIVE_E2E_RESULTS.md](LIVE_E2E_RESULTS.md).

This proves **positive read-only E2E through ChatGPT**. It does **not** provide
independent live process revision proof: a reported release path and file hashes
identify on-disk source, not the loaded revision of the serving process. Prior
[deployment identity evidence](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/DEPLOYMENT_RECEIPT.json)
and [restart observations](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/RESTART_PERSISTENCE.md)
cover process identity at their recorded observation times. This result does
not expand that proof to a new process observation.

## Rollback readiness

[ROLLBACK_PLAN.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/ROLLBACK_PLAN.md): verified target = immutable pre-cutover release
`2b123e21…` (intact on disk, module hashes recorded pre-change);
switch-back = installer `--release-dir <old> --apply` + daemon-reload +
restart, exact commands written and preflight-validated (installer `--check`
green with port free after stop).

## Remaining gates (conditions of this verdict)

1. **Reboot persistence — OPEN.** Not authorized in this order; not tested.
   Procedure documented in [RESTART_PERSISTENCE.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/RESTART_PERSISTENCE.md).
2. **External ChatGPT plugin E2E — PASS (positive read-only scope).**
   The previously open positive gate is satisfied by job
   `A3JOB-20261020-02403a2cb33e2efb93615351a941fbfb`; see
   [the receipt and proof limits](LIVE_E2E_RESULTS.md). No claim of independent
   live process revision verification or full signoff follows from this run.
3. **Interpretation condition (operator awareness, F01):** the contract keeps
   each same-turn `agent_message` replaces the pending answer; blank/textless
   final messages leave no answer. Removing the last event while retaining an
   earlier same-turn message lets that earlier message complete the corrected
   control. If the operator prefers a
   fail-closed rule for mid-turn message removal (not realizable by the
   native protocol), that is an explicit separate contract decision, not
   silently implemented here.

## Evidence provenance and inventory

The external bundle is retained without importing logs, deployment tools,
credentials or raw job output into Git. During finalization, 43 of the 44
historical manifest entries matched their recorded SHA256; only the temporary
`EVIDENCE_MANIFEST.json.tmp` entry was missing. The original report,
deployment receipt and restart evidence hashes all match the manifest.
The manifest is historical and does not cover these new tracked documents.

Key host-local evidence:

- [Evidence manifest](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/EVIDENCE_MANIFEST.json)
  (SHA256 `652082af749d1ea5235997981352f3683831c20bf471085ba78a23e44a3027e1`).
- [Deployment receipt](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/DEPLOYMENT_RECEIPT.json)
  (SHA256 `808e3e609d8edefc6b24905d0cbeea9adf0c9d51fa98d369339ec2bb695f6a5c`).
- [Restart persistence](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/RESTART_PERSISTENCE.md)
  (SHA256 `79fad331b138230291a30b3e47e5230e4437bd1eb82f7e1e80a3831a5d3ed766`).
- [Historical gateway E2E results](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/LIVE_E2E_RESULTS.md)
  (SHA256 `c09ad402676cf3eb2ef12be8c0afcb7e898181116a0da02984954fabf3484b93`);
  its ChatGPT OPEN statement is superseded for the positive read-only scope
  by [the new tracked receipt](LIVE_E2E_RESULTS.md).

### Historical directory contents

[BEFORE_AFTER_RECEIPTS.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/BEFORE_AFTER_RECEIPTS.md), [DEPLOYMENT_RECEIPT.json](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/DEPLOYMENT_RECEIPT.json),
`EVIDENCE_MANIFEST.json` (historical SHA256 inventory; temporary-entry exception above), [GITHUB_PR_AND_MERGE.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/GITHUB_PR_AND_MERGE.md),
`LIVE_E2E_RAW.json`, `LIVE_E2E_RESULTS.md`, [NATIVE_E2E_LOCAL.json](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/NATIVE_E2E_LOCAL.json),
[PARITY_TEST_MATRIX.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/PARITY_TEST_MATRIX.md), [REPRO_DEPLOYED.json](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/REPRO_DEPLOYED.json), `REPRO_POST_PATCH.json`,
`REPRO_PRE_PATCH.json`, `PRECHANGE_INVENTORY.json`, [RESTART_PERSISTENCE.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/RESTART_PERSISTENCE.md),
[ROLLBACK_PLAN.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/ROLLBACK_PLAN.md), [ROOT_CAUSE_AND_SCOPE.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/ROOT_CAUSE_AND_SCOPE.md), [SEMANTICS_CONTRACT.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/SEMANTICS_CONTRACT.md),
[TEST_RESULTS.md](/home/sscomp/AEE_CODEX_FAILURE_SEMANTICS_REMEDIATION_EVIDENCE_20261021/TEST_RESULTS.md), `logs/`, `tools/`, `native_e2e_stream.jsonl` (444).
