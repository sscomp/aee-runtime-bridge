# Current MCP runtime regression

Run the complete published portable suite with the command in
[deployment](../../docs/deployment.md). It uses compiled C executables, private
temporary stores/sockets and synthetic SSE responses. No real API credential,
model inference, production service or tunnel is required.

Coverage includes exact five-tool ASGI protocol/auth, future-tool rejection,
traversal/secret rejection, read-only namespaces, bounded processes, task-as-stdin,
leases/concurrency/restart, malformed records and completed-result proofs, broker
UID/lease/request policy, deployment gates, immutable Git packaging/metadata,
fixture-only migration/rollback and systemd offline parsing. The fresh-process
result reader consumes `/tmp` fixtures only. It is not an operator job-store reader.

Installed-native qualification is separate from portable CI. Without explicit
`AEE_TEST_NATIVE_CODEX`, broker/native provider checks skip with reason; the
optional comparison uses `AEE_TEST_UNREVIEWED_CODEX`. These paths must refer to
operator-reviewed native ELFs and companions. They drive synthetic upstream
responses, not production inference. A mode/owner test may skip when the account
has no alternate permitted group. Required protocol/policy/packaging cases are
never marked optional. CI fixtures are not evidence for production gate approval.

Real service cgroup containment, the exact Bubblewrap build, full native tool
failure behavior and host/account ChatGPT E2E must be qualified on the independent
Stage 3 host before approval. Historical development probes remain in Git history
and the preserved engineering source; they are not copied into this portable
publication or substituted with invented host evidence.
