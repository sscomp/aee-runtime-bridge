# Configurable job ID prefix

`AEE_JOB_ID_PREFIX` controls newly generated gateway job IDs. When unset it
remains `A3JOB`. For example, operators may explicitly choose `M2JOB` or
`N2JOB`; no hostname or deployment profile is consulted. The lower-level
`JobStore` accepts the same value as its keyword-only `job_id_prefix`
argument and defaults to `A3JOB` independently of the environment.

The exact prefix grammar is `[A-Z][A-Z0-9]{0,15}JOB`: a 1–16 character ASCII
node code beginning with an uppercase letter, followed by `JOB`. Values are
case sensitive, never trimmed or normalized. Empty values, slashes, dots,
whitespace, Unicode lookalikes and oversized values fail with `ValueError`
before store construction creates or changes files. The full ID remains
`<prefix>-<UTC YYYYMMDD>-<32 lowercase hexadecimal characters>`. Date and
128-bit random suffix generation are unchanged. Prefixes identify a namespace;
they provide no authentication or global coordination between independent stores.
Broker socket paths must still fit the platform Unix-domain socket length limit.

Readers accept every prefix in this grammar, independently of the generation
setting. This includes legacy `A3JOB` and newer `N2JOB` records, plus `M2JOB`
and future node codes. Store listing, active leases, restart reconciliation,
status/result/cancel, broker sockets, spool manifests and offline migration
inventory/backup/rollback all use this contract. Registry enumeration considers
`*JOB-*.json` candidates and rejects malformed candidates rather than overlooking
an active job. Integrity verification, ownership and lease enforcement remain in
place. Use the original complete ID when querying a record; changing the setting
does not rename, alias or reseal terminal records.

Change generation settings only at a planned restart with no active worker.
Restoring `AEE_JOB_ID_PREFIX=A3JOB` (or unsetting it) under this version still
reads all existing namespaces. **Reverting the source to a pre-feature version
is different:** older readers only understand `A3JOB` and can overlook newer
records. Keep the mixed-prefix reader when reverting configuration; any source
downgrade requires a separate offline review. This change performs no migration,
production configuration update or deployment.

## Offline regression from committed source

Use a fresh checkout of the feature commit and an isolated Python 3.13 environment
on Linux or macOS. Install the unchanged hashed runtime dependency closure:

```sh
uv venv --python 3.13 .venv
uv pip sync --python .venv/bin/python --require-hashes requirements-mcp.lock
TMPDIR=/tmp PYTHONPATH=.:tests/mcp .venv/bin/python -m unittest test_job_identity -v
.venv/bin/python -m compileall -q aee/mcp_runtime mcp_gateway.py
```

The new tests use private temporary stores and synthetic completion/cancellation
proofs; no real agent, gateway listener, credentials or provider call is needed.
They cover default and configured generation, A3/N2/M2/future namespaces,
persisted reads/listing, configuration rollback, unchanged legacy terminal bytes,
identical random suffixes across namespaces, invalid inputs, active leases,
interruption recovery, tool status/result/cancel, spool evidence, broker leases,
and fixture-only migration and backup-manifest rollback.

Canonical base `286ca6d` still has separate Linux-only assumptions: its owner
probe reads `/proc`, and its offline migration safety gate expects resolved
`/tmp` paths (macOS resolves these to `/private/tmp`). On macOS this focused
suite supplies an explicit synthetic owner and skips the two migration safety
cases; Linux exercises both unpatched. This prefix PR does not include the
separate N2 native macOS port and does not qualify a fresh macOS gateway for
real job execution. Linux sandbox/native-provider qualification remains governed
by the existing [deployment checks](deployment.md) and [runtime tests](../tests/mcp/README.md).
