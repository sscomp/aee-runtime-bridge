# Execution budget contract v1

Design recorded before implementation, base 255b779f. Generic adapters use a typed
ExecutionBudget; the separately approved native broker remains on its pinned
Limits/resource-profile contract. No restricted MCP schema or deployment gate is
expanded. Broker-path migration requires its own native qualification.

Precedence: built-in portable defaults < explicitly set legacy A3_CODEX timeout /
summary / log / result settings (migration fallback, all generic adapters) < policy
`defaults` < policy `adapters.<name>`. Policy is administrator-owned local JSON,
selected by AEE_EXECUTION_POLICY; no interpolation, includes, remote URLs or task
supplied budget. Read once at gateway startup; changes require an operator-approved
restart. Each admitted job snapshots validated policy and SHA256 revision.

Default wall deadline 3600s, no idle deadline, no max-turns; memory tail 65536 bytes
per stream, parser token capture 65536 bytes, stream quota 64 MiB, job quota 96 MiB,
total retained quota 512 MiB, inline result 16384 bytes, log excerpt 2000 bytes,
retention 86400s, concurrency 1. Values are job budgets within validated host safety
ceilings (wall up to 7 days, stream/job/total up to 1/2/8 GiB, memory 1 MiB,
inline result 65536, log 2000). Retain current single-flight admission; concurrency
other than 1 is rejected, never silently ignored. Host cgroups/sandbox limits are
independent; raising wall time does not raise CPU/memory permissions.

Only Claude accepts optional positive max_turns. Codex/Pi reject that setting.
Claude omission means CLI default, verified by installed help/source and E2E; no
implicit 16. Only `on_spool_full=fail` is supported. Claude switches to stream-json/verbose only when an idle deadline is enabled.
Idle deadline, when enabled, resets on valid adapter progress events (never arbitrary stdout/stderr); use with
care for long tools. Errors are explicit: EXECUTION_TIMEOUT,
EXECUTION_IDLE_TIMEOUT, TURN_BUDGET_EXHAUSTED, SPOOL_QUOTA_EXCEEDED,
SPOOL_IO_ERROR, PARSER_ERROR, EXECUTION_CANCELLED, EXECUTION_INTERRUPTED.

Budgets are not new containment. OS identity/working-directory access, native
Codex sandbox, Claude plan permissions, Pi read-tool restriction and deployment
policy remain boundaries. Unsupported adapter enforcement fails closed. There is
no source edit per host. See sample policy and migration/release guide.

The JSON parser additionally enforces bounded grammar/projection safety: nesting
64, 1024 retained container nodes, 256 entries per retained content array, retained
projected strings up to eight capture windows per event, raw token prefix up to
six capture windows. Large unknown strings/events are streamed and discarded from
projection, not from spool. Oversized final strings produce explicit output-limit
evidence. These bounds limit parser state; memory_capture_bytes is not a claim
that the Python interpreter or CLI uses only that many bytes of RSS.
