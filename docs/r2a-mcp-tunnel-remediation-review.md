# R2-A MCP tunnel consumer remediation — review gate

Status: **DRAFT / NOT DEPLOYED / NO RETIREMENT AUTHORIZED**

## Incident and confirmed test result

The Abacus-M2 MCP connector intermittently returned `unknown tool` for registered AEE tools. A reversible isolation test found that when a legacy embedded MCP stub sharing the production tunnel identity was paused, 20/20 calls to `aee_agents` and `aee_status` succeeded. After the stub resumed, the error reappeared. This strongly supports competing tunnel consumers as the operational cause. The current production route has **not** been permanently repaired.

## Proposed change

Retire the obsolete stub consumer **only after** its owner confirms no active turn or UI dependency, the operator approves the change window, and the exact target process identities have been refreshed. Do not modify the production gateway, authentication, A2 policy, runtime manifest, or broker as part of R2-A. Do not use SIGSTOP as a permanent fix.

## Release artifacts still required for code review

The current engineering handoff includes a host-specific retirement candidate and references to a publisher, snapshot launcher, and no-signal tests. The complete matched artifact set must be reviewed and committed together **only after sanitization and portability review**. In particular, do not commit host-specific PID/start-tick snapshots, private credentials, authorization records, user home/profile data, or machine fingerprints to this public repository. Use deploy-time configuration or a private release artifact for host-bound parameters.

The handoff identifies these candidate hashes (provenance only; not approval):
- Retirement candidate: `112bd4afbe90540675d6de18a84ab455650ea0cdbac7cbd04b680849ac8bb81c`
- Snapshot launcher: `0a6a0444026e57069c2fa447b98a930f2cead78f05e74b1db79b5e36cdc78669`
- Publisher: `09b9017ec16f57c1af9e10a12821361aedab81081928ca4ed3fbf02283d16c69`
- No-signal tests: `04f3b091551b53bafe8043f1e6ef47946828972d6bae95820ed7c514e8f71072`

These hashes do not establish that the referenced files are present in GitHub, deployable, or safe to execute.

## Required gates before apply

1. Owner release: verify no active Codex turn/UI dependency and authorize shutdown of the stub and its descendants.
2. Operator approval: separate, auditable approval for the specific target, code hash, and maintenance window.
3. Independent review: inspect publisher, launcher, retirement candidate, and test code as a coherent release.
4. Secure packaging: protect immutable release, public-key trust registry, lock inode, and signed approvals; no secrets in GitHub.
5. Dry run from an independent management session; validate pidfd support, identity, ancestry, health, and rollback limitations.
6. Apply only within authorized window; no `kill` fallback, no silent privilege widening.
7. Post-change validation: target processes and listener gone, no competing consumer, gateway/tunnel healthy, restricted six-tool contract without `aee_exec`, repeated remote calls successful, 120-second observation.
8. Record rollback scope: restoring the old stub may reintroduce the known tunnel conflict and cannot restore in-memory Codex turns.

## Separate acceptance tracks

- **R2-A MCP routing:** Not yet complete; requires approved retirement and remote MCP validation.
- **A2 Trusted Dispatch:** Separate G1–G8 and real dispatch/job status/result validation. Do not infer A2 readiness from R2-A.

## Pull request scope

This document captures the change-control requirements only. It intentionally does **not** contain a runnable retirement script or authorize production execution. Follow-up PR(s) should add a sanitized, reviewed implementation and tests.
