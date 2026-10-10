# Fresh-host bootstrap checklist (agent-friendly)

A single canonical route to a production-qualified restricted AEE v2 gateway on
a brand-new Linux host. Walk it top to bottom; each step's exit condition is
stated. Host- and operator-specific values (`/opt/aee`, numeric UIDs, unit
names, the approval id) are **examples** — substitute the reviewed values for
your host. Do not invent any value.

Prerequisites summary: Linux x86_64, systemd with cgroup v2, CPython 3.13.x via
`uv`, Git, GCC, `prlimit`, rootless capability to install
[`bwrap`](deployment.md#5-host-evidence-notes-provenance) at `/usr/bin/bwrap`,
loopback ports 8790/8791 free, and an operator-provisioned OpenAI API key file
(mode 0600, path recorded, value never echoed anywhere).

## Checklist

1. **Tooling (no root).** Install `uv` user-level; create the venv from the
   reviewed commit. Exit condition: `uv venv` succeeds and
   `uv pip sync --require-hashes requirements-mcp.lock` converges byte-identical
   on re-run. → [deployment §0–2](deployment.md#2-install-locked-dependencies-and-run-offline-smoke)
2. **Offline smoke + docs/secret gates (no root).** Run the MCP unittest suite
   and `scripts/check-canonical-docs.py`; both green. Exit condition: suite `OK`
   and docs validator `PASS`. Native-qualification tests that skip without
   operator-pinned paths are an expected, labeled SKIP.
3. **Pinned artifact provenance.** Verify the exact Bubblewrap artifact in
   `config/p2c/sandbox-profile.json` (source URL, release, sigstore attestation,
   SHA256) and the native Codex/companion digests in `provider_contract.py`
   against independently downloaded release artifacts. Exit condition: digests
   match documentation, artifact prints its documented identity. Re-pin policy:
   only with a reviewed artifact from a documented official source; never edit a
   digest to turn validation green. → [deployment §5](deployment.md#5-host-evidence-notes-provenance)
4. **Sandbox qualification.** Run the sandbox verifier and native isolation
   tests on the chosen artifact (namespace isolation, read-only binds, timeout/
   output limits, forbidden paths, exit codes). Exit condition: verifier PASS;
   any kernel/user-namespace restriction is a stop, not a switch to unsandboxed.
5. **Immutable release + accounts.** Build/verify/plan with the packager
   (`STAGE1_NOT_DEPLOYABLE` manifest), create `aee-gateway`/`aee-broker`/
   `aee-inference`, install the release under `/opt/aee/releases/<commit>` with
   pointer `/opt/aee/current`, venv from the release's hashed lock. Exit
   condition: release modes 0555/0444 root-owned and the verify step re-passes.
   → [deployment §3–4](deployment.md#3-build-verify-and-plan-an-immutable-release)
6. **Config with no placeholders.** Render `/etc/aee/gateway-restricted.env` and
   `/etc/aee/broker.env` privately; every `<...>` filled; gateway lister stays
   `127.0.0.1:8791`; job store outside the tree. Exit condition: no placeholder
   remains and mode is 0600 root-owned.
7. **Credential presentation.** For the per-0600-only broker policy, pick the
   LoadCredential route only if a live probe shows the broker user can read it;
   otherwise use the reviewed broker-owned 0600 file + `ExecStart=` drop-in.
   → [deployment §4a](deployment.md#4a-model-credential-presentation-on-modern-systemd-verified-pattern)
8. **Start + restricted surface check.** `systemd-analyze verify` then start
   broker and `aee-p2c-gateway@restricted`; `/health` 200; exactly the five
   remote tools; no `aee_exec`; auth negatives 401. **Stop here until a genuine
   operator review approves production** — startup without the reviewed
   manifest is the honest stop, never `AEE_RUNTIME_MANIFEST` removed.
9. **Production admission.** Render the deployment manifest with the genuine
   operator approval id and only fields verified by independent host evidence;
   record the manifest SHA256 separately. Exit condition: `APPROVED_STAGE2`
   manifest accepted by the running services. → [deployment §4](deployment.md#4-operator-qualification-and-installation)
10. **Completed-job smoke + persistence.** Run the completed-job smoke test
    ([deployment §4c](deployment.md#4c-completed-job-smoke-test-real-http-mcp-production-seal)):
    negatives, answer-only read-only dispatch, and at least one **tool-using**
    read-only dispatch that performs an actual exec operation; both must poll to
    a persisted `completed` with the `aee-completed-v1` seal (the tool-using
    record additionally carries an `operation_attestation` grade). Then gateway
    restart, re-fetch, and verify `validate_success` still accepts the records.
    Enable units and re-validate cold boot
    ([§4b](deployment.md#4b-service-lifecycle-and-boot-persistence-cold-boot-validated)).
    Exit condition: smoke PASS before → PASS after restart → PASS after reboot.
11. **Legacy cutover (only if a legacy bridge exists).** Retire the legacy units
    only after step 10 passes; see
    [operations, legacy bridge cutover](operations.md#legacy-bridge-cutover-aee-bridge--a2-aee-tracker)
    for inventory, backup, stop/disable order and rollback.

Failure semantics: any step's exit condition fails → stop, record the exact
command and output category in the operator-run report, and do not proceed with
workarounds that weaken validation, silence a negative test or alter a pin.