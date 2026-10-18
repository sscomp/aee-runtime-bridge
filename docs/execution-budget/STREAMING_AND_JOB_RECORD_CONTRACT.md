# Streaming and job record contract v1

Before spawning, create a job-scoped random artifact directory below the private
job store (0700 owner-only root/directories, 0600 files). Reject symlink / non-owner
boundaries and unsafe IDs. Process stdout/stderr independently via nonblocking
selectors, bounded reads and bounded memory tails. Incremental parsing retains
only relevant structured final/progress fields; oversized intermediate string
values are consumed without accumulating them. Final answer is extracted from
structured final events, never the last arbitrary tail. UTF-8 and JSON truncation
are deterministic parser failures. Pi requires authoritative message_end plus agent_settled and a successful EOF;
plain-text Pi is not accepted by the updated adapter. Gateway Pi uses JSON events
and disables MCP/extensions/skills. Claude accepts JSON document, event array and JSONL.

All spool accounting is serialized with a stable flock; active job leases prevent
cleanup. Per-stream, job and aggregate retained quotas are checked before each
write. Synchronous disk writes provide backpressure; quota/full disk errors kill
the process group and preserve bounded evidence. No silent byte discard claims:
observed / written / captured byte counts, SHA256 of actually written bytes and
fidelity/truncation flags describe each stream. Digests are independently
verifiable offline; no raw log download API is added to restricted MCP.

Terminal stream metadata is fsynced to an atomic manifest (temporary exclusive
file, fsync, replace, directory fsync), then attached under execution.streaming
inside the existing sealed record. Includes opaque artifact ID (no filesystem
path), elapsed, exit code, failure stage, policy version and bounded diagnostic
tails. Gateway redacts diagnostics before record/API persistence. Raw artifacts
are private, potentially sensitive, retained only for bounded operator inspection.
The existing artifacts=[] / 64 KiB record / 4000-char summary contracts stay intact.

Exactly one worker owns terminal transition. Cancellation observed before process
completion wins; after the runner accepts completion, normal terminal result wins.
Timeout/cancel/quota/parser/child failure retain metadata; cleanup always kills
whole process group, including descendants holding pipes after leader exit.

On restart JobStore lease reconciliation marks abandoned jobs EXECUTION_INTERRUPTED;
spool reconciliation finalizes orphan manifests from bounded files without retrying
jobs. Active spool leases are never reclaimed. Finished/orphan spools expire on
startup and subsequent admission cleanup; disk quotas include unexpired orphans.
No background service or changes to production records are required. Manifest
failure itself is SPOOL_IO_ERROR; physical exhaustion can prevent durable record
writes too, so operators must reserve independent state space and reconcile after
freeing disk. Evidence cannot promise durability when storage rejects all writes.
Development rollback removes only isolated worktree changes. Production/config
rollback and all service actions remain an explicit operator gate.

Quota definitions count raw stream bytes. Metadata/inodes have a separate mandatory
ceiling of 4096 artifact directories, one manifest <=16KiB per artifact and bounded
fixed files. Interrupted temporary manifest files are recognized for cleanup.
No arbitrary artifact retrieval tool/HTTP endpoint exists, so traversal and cross-
job artifact reads cannot be requested through MCP. Offline inspection requires
OS owner access and digest verification. Summary/log/tails may compact further
to fit the existing record byte ceiling; counters/digests survive compaction and
tail_omitted/truncated explicitly describe omissions. Process guardian uses Linux
PR_SET_PDEATHSIG and kills its whole group on gateway death; no privilege or
service management operation is needed.

Terminal diagnostic tails have an additional 512-byte-per-stream metadata ceiling
within log_excerpt_bytes, so control-character JSON escaping cannot overflow the
manifest. Recovery does not fabricate elapsed time or observed bytes after crash.
