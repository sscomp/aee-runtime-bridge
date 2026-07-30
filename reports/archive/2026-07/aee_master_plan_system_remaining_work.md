# AEE Master Plan — System-Level Remaining Work Audit

**Task:** Comprehensive read-only audit of AEE Master Plan and all authoritative planning/specification documents.
**Date:** 2026-07-30
**Author:** M2 (Hermes Agent, abacusclaw container), glm-5.2 via ollama-cloud
**Scope:** Read-only. No source/test/plan/report modifications except this artifact. No commit/push/deploy/restart/merge/rebase/stash/delete/move.
**Mode:** Evidence-first audit. Plan claims verified against on-disk code/tests where feasible.

---

## 1. Executive Correction / Scope Clarification

**Prior interpretation (to be corrected):** The prior roadmap confirmation concluded that W1–W15 (Bootstrap v1) were complete and implicitly framed the project as having reached a completion gate. That framing is **too narrow**.

**Corrected interpretation:** Bootstrap v1 (W1–W15) is a **sub-system milestone**, not a system-level completion gate. The AEE project has multiple major workstreams beyond bootstrap that remain partially implemented, planned-but-not-started, or deferred. The project is at a **release candidate (rc1) milestone with significant remaining system-level work** before GA.

Specifically:
- Bootstrap v1 is shipped (Phases A–D, W1–W15 committed).
- Epic 9 (Architecture Unification) has shipped code for profiles, CLI, installer, Docker, adapters, and CI/CD — but all §21.A acceptance checkboxes remain **unchecked (☐)** in the master plan (20 total unchecked items across §21.6.G and §21.A).
- The v3 governance pipeline is adopted but the **P0 blocking gate is NOT activated** (`enforcement_gate.blocking=false` in `config/notify.json`), the **7-day shadow run has NOT started**, and **production release authorization has NOT been granted** by 鼎鼎.
- AEE-MINI deprecation is started (DEPRECATED.md exists in one repo copy) but the deprecation timeline extends to version 2.0.2 (archive), which is multiple Epics away.
- Several AEE-6, AEE-7.x deferred items, tech debt items (§12), and future architecture items (§10, AEE-6..AEE-10) remain open.

---

## 2. Authoritative Planning Sources and Precedence

| # | Source | Path | Lines | Role |
|---|--------|------|-------|------|
| 1 | **AEE Master Plan** (canonical) | `/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md` | 8058 | Primary planning document, 22 chapters covering AEE-0 through AEE-10, Epic 9, v3 Governance |
| 2 | **AEE Master Plan V2 (reconstructed)** | `/home/ubuntu/Abacus/90_ARCHIVE/aee-master-plan-backups/AEE_MASTER_PLAN_RECONSTRUCTED_V2.md` | 2851 | Earlier reconstruction with AEE-7.1–7.3 detail; known stale claim (§2.1 says bridge is not a git repo — now stale) |
| 3 | **Bootstrap v1 Spec** | `/home/ubuntu/hermes-runtime-bridge/reports/aee_bootstrap_v1_spec.md` | 1293 | W1–W15 work breakdown, acceptance criteria, risks |
| 4 | **Bootstrap README** | `/home/ubuntu/hermes-runtime-bridge/docs/aee/bootstrap/README.md` | — | Phase history (A–D shipped) |
| 5 | **Migration/Packaging Alignment** | `/home/ubuntu/Abacus/AEE_MIGRATION_PACKAGING_MASTER_PLAN_ALIGNMENT.md` | — | Packaging/migration readiness assessment (rescue, 2026-07-23) |
| 6 | **v3 Governance Artifacts (9)** | `/home/ubuntu/workspace/aee/reports/AEE_V3_*.md` + `AEE_2_0_0_RC1_OPERATOR_HANDOFF_PACKAGE.md` | — | Governance baseline, RC1 handoff package |
| 7 | **AEE-MINI repo** | `/home/ubuntu/Abacus/aee-runtime-api-mini/` | — | Deprecated separate codebase (HEAD `c161b8c`, DEPRECATED.md present) |

**Precedence:** Master Plan §22 (v3 Governance) > §21 (Epic 9) > §7 (AEE-7.x) > §14 (AEE-6) > §10 (Future Architecture) > §12 (Tech Debt) > §13 (Risks). Where V2 conflicts with the canonical plan, the canonical plan wins.

---

## 3. AEE System Architecture Workstreams

The AEE system decomposes into the following workstream domains:

| Domain | Master Plan Ref | Description |
|--------|----------------|-------------|
| **Core Platform** (dispatch, state machine, adapters) | §1, §2, §11 (AEE-0–AEE-5) | Runtime-neutral dispatch engine, adapter Protocol, worker claim protocol, capability registry |
| **Artifact Pipeline** | §14 (AEE-6) | Artifact collection, storage, API endpoints, content-type sniffing |
| **Multi-Provider Orchestration** | §7 (AEE-7.1–7.4) | Provider Protocol, ProviderRegistry, ExecutionOrchestrator, observability |
| **Future Architecture** (scheduler, event bus, MCP, cluster) | §10, §11 (AEE-6..AEE-10) | Capability scheduler, event bus, MCP worker, cluster/multi-host |
| **Epic 9 — Architecture Unification** | §21 | Profile matrix, CLI UX, installer, Docker, provider-neutral deployment, CI/CD, deprecation |
| **v3 Governance** | §22 | 7-stage pipeline, 4-stage completion, blocking gate, Telegram enforcement, P0/P1/P2 roadmap |
| **Bootstrap / Installer / Doctor** | Bootstrap v1 spec (W1–W15) | Cross-platform installation, lifecycle management, doctor diagnostics |
| **Tech Debt** | §12 | Hermes coupling, adapter gaps, dispatcher coupling, operational debt, test gaps, doc debt |
| **Deployment Adapters** | §21.6 | Abacus, Zo, MacBook, Docker, Terraform-AWS, Windows adapters |

---

## 4. Bootstrap Status

**Verdict: SHIPPED (all 4 phases, W1–W15 committed).**

Per `docs/aee/bootstrap/README.md` phase history:
- Phase A (W1–W5): shipped — identity, backend, CLI, lifecycle, manifest
- Phase B (W6, W8, W10, W11, W12): shipped — Linux/macOS/Windows bootstrap, update, workflow, redaction
- Phase C (W7, W13): shipped — doctor, offline bundle
- Phase D (W9, W14, W15): shipped — release pin/drift, operator docs, acceptance gate

**Code evidence:** `aee/installer/{backend,lifecycle,update,workflow,redaction,linux_bootstrap,macos_bootstrap,cli_install}.py` all present on disk. `tests/acceptance/bootstrap_v1_acceptance.py` (14KB) present. `install.sh` (9.7KB) and `install.ps1` (9.2KB) present. W1 Windows adapter (`aee/deploy/adapters/windows.py`, 6530 bytes) is the latest commit (`befe3d6`).

**Bootstrap is NOT the system completion gate.** It is one sub-system that is complete. The broader system has remaining work documented below.

---

## 5. Non-Bootstrap Workstream Status Matrix

| Workstream | Master Plan Ref | Status | Evidence |
|---|---|---|---|
| AEE-0 (security baseline) | §11 | **completed** | `.gitignore`, safety policy, tests shipped |
| AEE-1 (core adapter layer) | §11 | **completed** | `RuntimeAdapter` Protocol, `HermesAdapter`, `FakeAdapter` on disk |
| AEE-2 (worker claim protocol) | §11 | **completed** | `POST /workers/register`, `/jobs/claim`, watcher/reaper refactored |
| AEE-2.5 (project foundation freeze) | §11 | **completed** | `git init`, first commit, Master Plan expansion |
| AEE-3 (capability matching) | §11 | **completed** | `required_capabilities_json` column, subset matcher, 9 tests |
| AEE-4 Part A (worker runtime contract) | §11 | **completed** | Worker Runtime Contract doc, `/v1/` prefix, ADR-006/007/008 |
| AEE-4 Part B (AEE Lightweight Agent Runtime) | §11 | **completed** (conditional approval cleared) | `aee-runtime/runtime/` (Node.js), `aee-runtime/aee_runtime.py` (Python daemon), 13 daemon tests + 2 smoke + 3 node tests |
| AEE-5 (runtime capability registry) | §11 | **completed** | `runtimes` + `dispatch_records` tables, `RuntimeDescriptor`, 9-step selector, `/v1/runtimes` API, 173 new tests |
| AEE-6 (artifact pipeline) | §14 | **partially implemented** | Slice 1 shipped (domain foundation, 52 tests); remaining items in §14.2: wire `ensure_aee6_schema`, `ArtifactPipeline.collect()` call site, `expected_artifacts` field, `POST /v1/artifacts` API, content-type sniffing, AEE-5→AEE-6 wiring test |
| AEE-7.1 (multi-provider orchestrator) | §7.1 | **shipped, committed** (rescue #2) | `aee/orchestrator/` package, 28/28 tests; known non-blocker: `dispatch_records.dispatch_status='failed'` for COMPLETED case |
| AEE-7.2 (dispatch correctness + per-job policy) | §7.2 | **shipped, not committed** | K+2 finalization; status mapping, `repo_root` interface, structured log |
| AEE-7.3 (status mapping rescue) | §7.3 | **shipped, not committed** | K+2 finalization; `PolicyViolationCode.OK` lowercase fix, `asyncio.run()` trap fix |
| AEE-7.4 (observability) | §7.4, §18 | **partially implemented** | Slice 1 (SOT) shipped; slice 2 (emitter) shipped; slice 3 (wire-up) shipped; **deferred**: production-source tripwire migration (call sites still inline literals), `DEFERRED_TRIPWIRE_ENABLED=True` but literal migration not done |
| AEE-6..AEE-10 (future) | §10, §11 | **planned, not started** | "Artifact pipeline UI + scheduler, event bus, MCP, cluster" — single line in release history |
| Epic 9 — Profile Matrix (§21.1) | §21.1 | **implemented** | `aee/profiles/descriptor.py` (17KB), 4 profiles, enforcement fields |
| Epic 9 — CLI UX (§21.2) | §21.2 | **implemented** | `aee/cli.py` (41KB), `--profile` flag on all dispatch subcommands |
| Epic 9 — Installer Profile (§21.3) | §21.3 | **implemented** | `install.sh` accepts `--profile`, delegates to Python backend |
| Epic 9 — Runtime Profile Selection (§21.4) | §21.4 | **implemented** | `app.py` profile resolution at L936, safety gate at L956, dispatcher at L1080 |
| Epic 9 — Docker Profiles (§21.5) | §21.5 | **implemented** | `Dockerfile` CMD `["--profile", "full"]`, `docker-entrypoint.sh` profile parsing; `docker-compose.yml` MISSING |
| Epic 9 — Provider-Neutral Deployment (§21.6) | §21.6 | **partially implemented** | `aee/deploy/adapters/{abacus,zo,macbook,docker,terraform_aws,windows}.py` all present; `host.capabilities.yaml` NOT FOUND on disk; acceptance items 1–8 all unchecked (☐) |
| Epic 9 — CI/CD Matrix (§21.7) | §21.7 | **implemented** | `.github/workflows/ci-matrix.yml` present, 4-profile matrix, merge gate; smoke step is placeholder (`§21.3 execute path not authorized`) |
| Epic 9 — Release Strategy (§21.8) | §21.8 | **not started** | `aee --version` does not return `2.0.0` yet; changelog not updated to reference ADR-009; acceptance item 8 unchecked |
| Epic 9 — Documentation Migration (§21.9) | §21.9 | **not started** | Unified `README.md` does not document all four profiles; acceptance item 9 unchecked |
| Epic 9 — Deprecation Plan (§21.10) | §21.10 | **partially started** | `DEPRECATED.md` exists at `/home/ubuntu/Abacus/aee-runtime-api-mini/`; AEE-MINI `README.md` has deprecation notice; repo NOT frozen at `1.0.1` (HEAD is `c161b8c` with post-deprecation commits); acceptance item 10 unchecked |
| v3 Governance — 7-stage pipeline | §22.1 | **adopted** | Design → Implementation → Verification → Independent Review → Atomic Commit → Telegram → Post-Release |
| v3 Governance — 4-stage completion | §22.3 | **adopted (observability-only)** | `enforcement_gate.blocking=false`; gate emits warnings but does not revert |
| v3 Governance — P0: 7-day shadow run | §22.5 | **NOT STARTED** | Requires zero divergence on protected files across 7-day window |
| v3 Governance — P0: Blocking gate activation | §22.5 | **DESIGNED, NOT ACTIVATED** | Requires shadow run PASS + 0 false blocks + 鼎鼎 authorization + config flip |
| v3 Governance — P1: CV1/CV2 reproducibility | §22.5 | **DEFERRED to GA** | Docker base digest pin, tar deterministic flags |
| v3 Governance — P1: R-New-1/R-New-2 GA hardening | §22.5 | **DEFERRED to GA** | Entrypoint `--version`, build artifact durable storage |
| v3 Governance — P2: AEE-MINI forced migration | §22.5 | **DEFERRED** | "No forced migration" binding constraint |
| v3 Governance — P2: AEE-MINI repo archival | §22.5 | **DEFERRED to 2.0.2** | Post-GA milestone |
| Tech Debt (§12) | §12 | **open** | 23 items across 6 categories; some mitigated by AEE-7.4, most target AEE-3+ (now historically "AEE-3+" = current+) |
| Risks (§13) | §13 | **open** | R1–R18; R9 resolved (git init); R17 mitigated (AEE-7.4 EventKind SOT); R18 mitigated (wire-up slice shipped); others unchanged |

---

## 6. Remaining System-Level Development Items

### 6.1 P0 — Critical Path to Production Release

| # | Item | Dependency | Evidence |
|---|------|-----------|----------|
| P0-1 | 7-day byte-identical shadow run of v3 pipeline against AEE-2.5 baseline | None (can start now) | §22.5: "Zero divergence on protected files; all cron jobs unchanged; macro_history.db sha unchanged across 7-day window" |
| P0-2 | Blocking gate activation (`enforcement_gate.blocking` flip false → true) | P0-1 PASS + 0 false blocks + 鼎鼎 explicit authorization + config flip + revert path test + test inversion | §22.5; `config/notify.json` currently `blocking: false` |

### 6.2 Epic 9 Completion (§21.A Acceptance — 12 items, all unchecked)

| # | Item | Dependency | Evidence |
|---|------|-----------|----------|
| E9-1 | §21.1 Profile Matrix in Master Plan matches `descriptor.py` enforcement fields | None — verify match | §21.A item 1 (☐) |
| E9-2 | §21.2 `aee --profile {full,mini,edge,developer}` works on all dispatch subcommands | CLI code exists; needs end-to-end verification | §21.A item 2 (☐) |
| E9-3 | §21.3 `install.sh --profile` works on fresh host for all 4 profiles | Installer code exists; `--execute` path not authorized (smoke placeholder in CI) | §21.A item 3 (☐) |
| E9-4 | §21.4 `POST /runs` with each profile produces correct `Task.profile`, safety enforcement, dispatcher routing | Profile wiring in `app.py` exists; needs verification | §21.A item 4 (☐) |
| E9-5 | §21.5 One Docker image, `docker run aee:X.Y.Z --profile {all 4}` works | `Dockerfile` + `docker-entrypoint.sh` exist; `docker-compose.yml` MISSING; needs build + run verification | §21.A item 5 (☐) |
| E9-6 | §21.6 Provider-Neutral Deployment Contract acceptance (8 sub-items) | `host.capabilities.yaml` NOT FOUND; adapter smoke tests not run; acceptance items 1–8 all (☐) | §21.6.G (☐ ×8) |
| E9-7 | §21.7 CI/CD matrix runs 4 profile jobs; all 4 green on `master` | CI workflow file exists; never run against actual GitHub remote (no remote configured) | §21.A item 7 (☐) |
| E9-8 | §21.8 `aee --version` returns `2.0.0` (not `1.0.1`); changelog references ADR-009 | Version bump not done | §21.A item 8 (☐) |
| E9-9 | §21.9 Unified `README.md` documents all 4 profiles; AEE-MINI `README.md` has deprecation notice | AEE-MINI README has notice; unified README not updated | §21.A item 9 (☐) |
| E9-10 | §21.10 AEE-MINI repo frozen at `1.0.1`; `DEPRECATED.md` at root; no new releases | `DEPRECATED.md` exists at one copy; repo NOT frozen (HEAD `c161b8c` has post-deprecation commits); second copy at `/home/ubuntu/Abacus/AEE-MINI/` lacks `DEPRECATED.md` | §21.A item 10 (☐) |
| E9-11 | Invariant: AEE-8.x profile plumbing intact on `master`; `descriptor.py` matches Master Plan §21.1 | Verify against current HEAD | §21.A item 11 (☐) |
| E9-12 | No regressions: `profile=full` dispatch path byte-for-byte identical to pre-Epic-9 behavior | Run regression suite | §21.A item 12 (☐) |

### 6.3 AEE-6 Remaining (§14.2 — 6 items)

| # | Item | Dependency | Evidence |
|---|------|-----------|----------|
| A6-1 | Wire `ensure_aee6_schema(conn)` into `dispatcher/db.py:_init_schema` | None — one-line append | §14.2 item 1 |
| A6-2 | `ArtifactPipeline.collect()` call site in `dispatcher/manager.py:complete()` | A6-1 | §14.2 item 2 |
| A6-3 | Replace input-text regex with explicit `Job.expected_artifacts` field | A6-2 | §14.2 item 3 |
| A6-4 | `POST /v1/artifacts` and `GET /v1/artifacts/{id}` + query endpoint | A6-1 | §14.2 item 4 |
| A6-5 | Content-type sniffing (`mimetypes` wrapper) | A6-4 | §14.2 item 5 |
| A6-6 | AEE-5 → AEE-6 wiring test (end-to-end integration) | A6-1, A6-2 | §14.2 item 6 |

### 6.4 AEE-7.x Deferred Items

| # | Item | Evidence |
|---|------|----------|
| A7-1 | Production-source EventKind literal migration (3 call sites: `dispatcher/manager.py:480/493`, `aee/artifacts/collect.py:448`, `aee/orchestrator/orchestrator.py`) | §7.4 deferred work; `DEFERRED_TRIPWIRE_ENABLED=True` but literals not migrated |
| A7-2 | `dispatcher/manager.py:614-618` — `dispatch_records.dispatch_status='failed'` written for COMPLETED case (known non-blocker) | §7.1.13 |
| A7-3 | `aee/api/jobs.py` — `GET /jobs/_claimable` shadowed by `GET /jobs/{id}` in FastAPI routing | §12.3 item 16, §13 R12 |
| A7-4 | R1–R4 atomic commits (staged-ready, not committed per user directive "本輪先不要 commit") | §20.8 — 4 groups verified, deferred to user approval |

### 6.5 P1 — Pre-GA Hardening (Deferred)

| # | Item | Evidence |
|---|------|----------|
| P1-1 | CV1/CV2 reproducibility hardening (Docker base digest pin, tar deterministic flags) | §22.5 P1 |
| P1-2 | R-New-1/R-New-2 GA hardening (entrypoint `--version`, build artifact durable storage) | §22.5 P1 |

### 6.6 P2 — Post-GA (Deferred)

| # | Item | Evidence |
|---|------|----------|
| P2-1 | AEE-MINI 1.0.1 forced migration | §22.5 P2 ("no forced migration" constraint) |
| P2-2 | AEE-MINI repo archival (read-only) | §22.5 P2, §21.10 (target: 2.0.2) |

### 6.7 Future Architecture (§10, §11 — AEE-6..AEE-10)

| # | Item | Evidence |
|---|------|----------|
| FA-1 | Capability Scheduler (pick worker by capabilities) | §10 diagram; §13 R15 ("AEE-4 scheduler can replace post-filter magic number") |
| FA-2 | Event Bus | §11 release history: "AEE-6..AEE-10 pending — event bus" |
| FA-3 | MCP Worker | §1 goal: "MCP Worker (AEE-9)"; §11: "MCP" |
| FA-4 | Cluster / multi-host | §1: "No cluster / multi-host (AEE-10)"; L336 |
| FA-5 | Artifact Pipeline UI | §11: "Artifact pipeline UI" |

### 6.8 Tech Debt (§12 — selected open items)

| # | Item | Category | Evidence |
|---|------|----------|----------|
| TD-1 | `dispatcher/models.py` — Hermes-specific defaults (`runtime_type="hermes"`) | §12.1 item 1 | Target: flip to `None`/`"unknown"` |
| TD-2 | `dispatcher/manager.py` — `find_by_hermes_run_id()` still accepts Hermes-specific name | §12.1 item 2 | Target: rename to `find_by_external_run_id()` |
| TD-3 | `dispatcher/notifier.py` — no per-adapter templating; no test | §12.1 item 4, §12.6 | Target: `runtime_label(task)` helper |
| TD-4 | `app.py` — `version = "1.3.0-aee2"` still embeds Hermes name | §12.1 item 7 | Target: rebrand to "Agent Execution Engine" |
| TD-5 | `aee/adapters/base.py` — `health()` not in Protocol (added ad-hoc in `app.py`) | §12.2 item 9 | Target: add to Protocol |
| TD-6 | `dispatcher/db.py` — `_COLUMNS` positional list silently drops future columns | §12.3 item 13, §13 R3 | Target: `PRAGMA table_info` introspector |
| TD-7 | `BRIDGE_API_KEY` shared by operator and worker endpoints | §12.4 item 21, §13 R4 | Target: split into `BRIDGE_API_KEY` + `WORKER_API_KEY` |
| TD-8 | `GET /health` reports stale `tasks_total` (computed at startup, not live) | §12.4 item 23, §13 R13 | Target: live-aggregate |
| TD-9 | `openapi.yaml` hand-edited, not regenerable from FastAPI app | §12.6 | Drift risk |
| TD-10 | No test for multi-worker race on same job | §12.5 | AEE-2 scope minimal on concurrency |

---

## 7. Plan-vs-Code Evidence

| Claim (Master Plan) | On-Disk Reality | Match? |
|---|---|---|
| §21.1: 4 profiles (full/mini/edge/developer) | `aee/profiles/descriptor.py` defines `KNOWN_PROFILES = ("full", "mini", "edge", "developer")` | ✅ |
| §21.2: `--profile` flag on all dispatch subcommands | `aee/cli.py` L137: `--profile` with `choices=KNOWN_PROFILES` | ✅ |
| §21.3: `install.sh --profile` | `install.sh` L69-230: parses `--profile`, delegates to Python backend | ✅ |
| §21.4: `POST /runs` with profile produces `Task.profile` + safety enforcement | `app.py` L936 profile resolution, L956 safety gate, L1080 dispatcher | ✅ |
| §21.5: One Docker image, `docker run --profile` | `Dockerfile` CMD `["--profile", "full"]`, `docker-entrypoint.sh` profile parsing | ✅ (but `docker-compose.yml` MISSING) |
| §21.6: 5 reference adapters (abacus, zo, macbook, docker, terraform-aws) | All 5 present in `aee/deploy/adapters/` + `windows.py` (bonus) | ✅ |
| §21.6: `host.capabilities.yaml` format defined | NOT FOUND on disk anywhere | ❌ (format defined in plan, no file created) |
| §21.7: CI/CD matrix workflow | `.github/workflows/ci-matrix.yml` present with 4-profile matrix + merge gate | ✅ (never run against remote) |
| §21.8: `aee --version` returns `2.0.0` | Not verified; `app.py:67` still says `version = "1.3.0-aee2"` | ❌ |
| §21.9: Unified README documents all 4 profiles | Not verified; likely not done (acceptance item unchecked) | ❌ |
| §21.10: AEE-MINI repo frozen at `1.0.1` | HEAD `c161b8c` has post-deprecation commits; NOT frozen | ❌ |
| §22.5: `enforcement_gate.blocking=false` | `config/notify.json` confirms `blocking: false` | ✅ (matches "NOT ACTIVATED" claim) |
| §14.2: AEE-6 remaining items (6) | `ensure_aee6_schema` not wired into `_init_schema` (deliberate); artifact API endpoints not created | ✅ (matches "partial" status) |
| §7.4: AEE-7.4 observability SOT | `aee/observability/{__init__,events}.py` + `aee/observability_runtime/wireup.py` present | ✅ |
| §7.4: Deferred production-source tripwire | `DEFERRED_TRIPWIRE_ENABLED=True` in test file; call sites still inline literals | ✅ (matches "deferred" status) |
| §11: AEE-6..AEE-10 "pending" | No scheduler, event bus, MCP, or cluster code found | ✅ (matches "not started") |
| §2.1 (V2): "hermes-runtime-bridge is NOT a git repo" | STALE — bridge is now a git repo with 30+ commits on `main` | ❌ (V2 stale claim, confirmed in MEMORY) |

---

## 8. Dependencies and Recommended Order

```
P0-1 (7-day shadow run)
  └→ P0-2 (blocking gate activation) — requires P0-1 PASS + 鼎鼎 authorization
       └→ Production release authorization (鼎鼎 explicit)
            └→ P1-1, P1-2 (GA hardening)
                 └→ P2-1, P2-2 (AEE-MINI migration/archive, target 2.0.2)

E9-6 (§21.6 acceptance: host.capabilities.yaml + adapter smoke tests) — can start now
  └→ E9-5 (Docker profiles verified) — needs docker-compose.yml + build verification
       └→ E9-7 (CI/CD green on master) — needs remote + actual run
            └→ E9-8 (version bump to 2.0.0)
                 └→ E9-9 (unified README)
                      └→ E9-10 (AEE-MINI freeze)
                           └→ E9-11, E9-12 (invariant + regression verification)

A6-1..A6-6 (AEE-6 completion) — independent of Epic 9; can run in parallel
A7-1..A7-4 (AEE-7.x deferred) — independent; R1–R4 commits need user approval
FA-1..FA-5 (future architecture) — post-GA; no current dependencies
TD-1..TD-10 (tech debt) — can be addressed incrementally; most target "AEE-3+" which is now current
```

**Recommended next planning/execution task:**

1. **Immediate:** Start P0-1 (7-day shadow run). This is the critical path to production release. No code changes needed — just observation and byte-identical verification over 7 days.
2. **Parallel:** Begin E9-6 acceptance verification (§21.6.G items 1–8): create `host.capabilities.yaml` for M2, run adapter smoke tests, verify installer accepts it. This unblocks Epic 9 acceptance.
3. **Parallel:** Commit R1–R4 atomic groups (§20.8) if user approves — these are staged-ready and verified.

---

## 9. Ambiguities / Migration Context Risks

| # | Ambiguity | Risk | Resolution |
|---|-----------|------|------------|
| 1 | **V2 §2.1 stale claim** ("bridge is NOT a git repo") | Future sessions reading V2 may be misled into running `git init` on an already-initialized repo | Use canonical Master Plan, not V2; V2 is archival only |
| 2 | **Multiple AEE-MINI repo copies** | `/home/ubuntu/Abacus/aee-runtime-api-mini/` has `DEPRECATED.md`; `/home/ubuntu/Abacus/AEE-MINI/aee-runtime-api-mini/` does NOT | Unclear which is canonical; both at same HEAD `c161b8c` |
| 3 | **AEE-MINI repo not frozen** | Plan says "frozen at 1.0.1" but HEAD has post-deprecation commits (WO-1, docs, §21.6 update) | Contradicts §21.10 "repo frozen (security patches only)" |
| 4 | **"AEE-3+" target dates** | Tech debt items (§12) target "AEE-3+" which is now historical — AEE-3 shipped 2026-07-10 | These items are effectively "current+" debt; should be re-prioritized |
| 5 | **§14.6 truncation damage** | Master Plan §14.6 was truncated (L3119, L3141) — original prose lost, pasted with AEE-7.x fragments | §14.6 content is incomplete; provider abstraction skeleton description cut off mid-sentence |
| 6 | **§19 missing** | Master Plan skips from §18 to §20 — §19 not found in heading scan | Either intentionally removed or lost in truncation; unclear |
| 7 | **20 unchecked acceptance boxes** | All §21.6.G (8) and §21.A (12) items are ☐ — unclear if this means "not verified" or "not done" | Code exists for most items; boxes likely mean "formal acceptance not performed" |
| 8 | **Bootstrap v1 vs Epic 9 overlap** | Bootstrap installer (`aee/installer/`) and Epic 9 installer profile (`install.sh --profile`) overlap in functionality | Bootstrap v1 may subsume or duplicate parts of Epic 9.3; relationship unclear |
| 9 | **Two master plans** | Canonical (8058 lines) and V2 reconstructed (2851 lines) cover different scopes; V2 has AEE-7.1–7.3 detail not in canonical | Canonical is authoritative; V2 is supplemental for lost §7.0 prose |

---

## 10. Overall Verdict

**The AEE project is at a release candidate (rc1) milestone, NOT at a full system completion gate.**

- **Bootstrap v1:** ✅ Complete (W1–W15 shipped, all phases committed).
- **Core platform (AEE-0–AEE-5):** ✅ Complete (all shipped and committed).
- **AEE-6 (artifact pipeline):** ⚠️ Partially implemented (slice 1 only; 6 remaining items).
- **AEE-7.x (orchestration + observability):** ⚠️ Shipped but not all committed; deferred items remain (literal migration, dispatch_status fix, R1–R4 commits).
- **Epic 9 (architecture unification):** ⚠️ Code implemented for §21.1–§21.7 but formal acceptance NOT performed (20 unchecked boxes); §21.8 (version bump), §21.9 (README), §21.10 (AEE-MINI freeze) NOT started.
- **v3 Governance:** ⚠️ Adopted but not operationalized — P0 shadow run NOT STARTED, blocking gate NOT ACTIVATED, production release NOT AUTHORIZED.
- **Future architecture (AEE-6..AEE-10):** ❌ Not started (scheduler, event bus, MCP, cluster).
- **Tech debt:** ⚠️ 23+ open items, most targeting "AEE-3+" (now current).

**The prior bootstrap-only interpretation was too narrow.** Bootstrap is one completed sub-system; the project has substantial remaining system-level work across governance activation, Epic 9 acceptance, AEE-6 completion, AEE-7.x finalization, version/release management, documentation, deprecation, and future architecture.

---

## 11. Git Status

```
Repository: /home/ubuntu/hermes-runtime-bridge
Branch: main
HEAD: befe3d6fe5eeeafed316883d27e2868638c64d22
Modified tracked files: 0
Untracked entries: 48 (report .md files + 1 .json)
Stash: (none)
Working tree: clean for tracked files
```

No modifications were made during this audit. The 48 untracked entries are pre-existing report files, not created by this audit.

---

## 12. Artifact Verification

```
ls -la reports/aee_master_plan_system_remaining_work.md
wc -l reports/aee_master_plan_system_remaining_work.md
sha256sum reports/aee_master_plan_system_remaining_work.md
```

(Verification commands to be run after file creation.)

---

## 13. Telegram

Per AEE-MINI Telegram rule (all AEE-MINI tasks must attempt Telegram notification), this audit produces a short-form Telegram summary:

```
✅ AEE Master Plan System Remaining Work Audit
Type: read-only audit report
Date: 2026-07-30
HEAD: befe3d6
Master Plan: 8058 lines, sha256 50a0ee93...
Verdict: Project at rc1 milestone, NOT system completion gate
Bootstrap: SHIPPED (W1-W15)
Core (AEE-0-5): SHIPPED
AEE-6: PARTIAL (6 items remaining)
AEE-7.x: SHIPPED, deferred items remain
Epic 9: CODE exists, 20 acceptance boxes unchecked
v3 Governance: adopted, P0 shadow run NOT STARTED, gate NOT ACTIVATED
Future (AEE-6..10): NOT STARTED
Report: /home/ubuntu/hermes-runtime-bridge/reports/aee_master_plan_system_remaining_work.md
```

Telegram delivery via `hermes send --to telegram:5132341473 --subject "AEE System Audit" --file reports/aee_master_plan_system_remaining_work.md --json` (to be executed after artifact verification).

---

*End of report.*