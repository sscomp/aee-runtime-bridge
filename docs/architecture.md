# AEE v2 architecture

ChatGPT → OpenAI Secure MCP Tunnel → host `tunnel-client` → authenticated
loopback restricted gateway → leased job → isolated native Codex → per-job Unix
socket broker → OpenAI Responses. Results return through MCP and the tunnel.
The host job store retains private records; ChatGPT receives bounded redacted
projections. See [deployment](deployment.md) and [privacy](security-model.md).

| Component | Source | Contract |
|---|---|---|
| Gateway | `mcp_gateway.py` | Restricted five tools on 8791; local/full on 8790 |
| Admission and store | `aee/mcp_runtime/store.py` | One active lease; bounded private JSON; restart handling |
| Executor and sandbox | `aee/mcp_runtime/executor.py`, `sandbox.py`, `process.py` | Trusted immutable source, stdin task, namespaces, finite output/time/process limits |
| Result contract | `aee/mcp_runtime/result_contract.py` | Exit zero alone is insufficient; independent completion proof; failures sticky |
| Broker | `aee/mcp_runtime/broker.py` | Dedicated credential owner; per-job socket, UID/lease and request policy |
| Packaging and deployment | `aee/mcp_runtime/packaging.py`, `config/p2c/systemd/` | Committed source, immutable metadata, closed default approval gate |
| Compatibility product | `app.py`, `aee/cli.py`, `aee/adapters/`, `install.sh` | Existing HTTP/CLI profiles and deployed-host migration remain maintained |

Agent discovery can report Hermes or Claude installed. This does not authorize
remote dispatch to them. Restricted dispatch accepts only the reviewed Codex
read-only execution profile; parameters cannot select arbitrary models, commands
or unrestricted modes. Arbitrary local `aee_exec` is never exposed to ChatGPT.

Gateway and broker run as different non-login accounts. The gateway can request a
short-lived broker lease but never receives the inference credential. Codex sees
only an admitted read-only snapshot, a private home and a private inference relay.
No host credential directory or system-manager socket enters its namespace.

Production startup and admission require native/companion identity, resource and
sandbox revisions, enforced inherited cgroup bounds, verified failure semantics
and an operator approval identifier. The fixture path without a deployment
manifest exists for isolated tests; it is not a production installation path.

Completed jobs require the `aee-completed-v1` proof at write, read and MCP result
boundaries. The record SHA detects accidental modifications; it is not protection
against an attacker who can rewrite both record and digest. Code Mode accepted
success is currently unsupported when expected inner operations cannot be
independently corroborated. See [limitations](troubleshooting.md).
