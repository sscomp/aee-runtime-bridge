# AEE Runtime Bridge — README Readiness & Documentation Inventory

**Task:** READ-ONLY README readiness and documentation inventory
**Repository:** `/home/ubuntu/hermes-runtime-bridge` (branch: `main`)
**Date:** 2026-07-31 (Asia/Taipei)
**Author:** Hermes M2 (Abacus.ai container, glm-5.2 via ollama-cloud)
**Mode:** READ-ONLY. No commit, push, deploy, restart, workflow trigger, dependency installation, or runtime mutation.

---

## 1. Execution Timing

| Field | Value |
|-------|-------|
| Start (UTC) | 2026-07-30T~17:35Z |
| Start (CST) | 2026-07-31 ~01:35 CST |
| End (UTC) | 2026-07-30T~17:55Z |
| Duration | ~20 minutes |

---

## 2. Repository State Evidence

| Field | Value |
|-------|-------|
| Branch | `main` |
| HEAD | `a9559a59e67d3d3222c2770c82da57127f043230` |
| HEAD subject | `fix(ci): target main branch workflows` |
| Working tree | Dirty (untracked files only — no tracked modifications) |
| Tracked modifications | 0 |
| Untracked files | ~120 root-level `.md` report files + `reports/*.md` + `scripts/` + `requirements*.in/.lock` + `constraints.txt` + `AEE_7_7d_7e_MANIFEST.json` |
| Remote | `origin/main` (up to date) |

**Recent commits (last 10):**
```
a9559a5 fix(ci): target main branch workflows
b8a6dd2 feat(aee): add docker compose profiles
ac23def fix(aee): suppress ghost task notifications
f6ae964 feat(aee): validate installer host capabilities
befe3d6 feat(bootstrap): add W1 — WINDOWS identity + WindowsAdapter skeleton
88788e5 feat(bootstrap): add Phase 7 Phase D — Hardening (W9 + W14 + W15)
a729cd3 feat(bootstrap): add Phase 6 Bootstrap v1 Phase C — Windows (W7/W13)
522c2af feat(bootstrap): add Phase 5 Bootstrap v1 Phase B (W6/W8/W10/W11/W12)
0b24ab7 feat(aee): add Phase 4D cross-slice integration tests (§21.4 approved)
589c299 feat(aee): Phase 4C update CLI surface
```

---

## 3. Shadow-Run Non-Interference Confirmation

| Check | Evidence | Disturbed? |
|-------|----------|------------|
| `data/dispatcher.db` mtime | `1785399377` (read-only stat, no write) | NO |
| `data/dispatcher.db-wal` mtime | `1785399842` (read-only stat, no write) | NO |
| `logs/shadow_run/baseline.json` mtime | `1785346712` (read-only stat, no write) | NO |
| Git history touching `logs/shadow_run/` | Empty (no commit ever touched shadow_run logs) | NO |
| Tracked-file modifications | 0 (`git diff --stat` empty) | NO |
| Process mutation | No `uvicorn`/`bridge` process started or stopped | NO |
| File writes | Only this artifact (`reports/aee_readme_readiness_inventory.md`) | N/A — new file, does not touch shadow run |

**Verdict:** P0-1 shadow run was not disturbed. All operations were read-only (`cat`, `head`, `ls`, `stat`, `git log/status/diff`).

---

## 4. Documentation Inventory — Inspected Paths

### 4.1 Existing README

| Path | Size | Lines | Status |
|------|------|-------|--------|
| `README.md` | 11,987 B | 291 | EXISTS — comprehensive, already covers profiles, endpoints, install, safety, tests, layout |

**README.md current sections:**
1. Title + architecture diagram
2. Why this exists
3. Product Surface — four profiles (§21.1)
4. Selecting a profile (§21.2, §21.3, §21.5)
5. Adapter contract
6. Endpoints
7. Local run (foreground)
8. Production run (Abacus.AI host — supervisord)
9. Cloudflare Tunnel
10. Safety guard
11. AEE targeted tests
12. Test scripts
13. Layout
14. What's intentionally not in v1
15. DO NOT pack runtime data
16. Authoritative references
17. Migration from AEE-MINI

### 4.2 Authoritative Documentation (in-repo, tracked or untracked)

| Path | Category | Evidence-Backed? |
|------|----------|------------------|
| `README.md` | Top-level README | YES — 291 lines, covers profiles/endpoints/install/safety |
| `docs/HERMES_ADAPTER_CONTRACT_MATRIX.md` | Adapter contract | YES — referenced by README as canonical |
| `docs/MIGRATION_FROM_AEE_MINI.md` | Migration guide | YES — operator migration guide AEE-MINI → unified |
| `docs/AEE_RUNTIME_INTEGRATION_GUIDE.md` | Integration guide | YES — AEE-4 Part B freeze |
| `docs/runtime/Worker_Runtime_Contract.md` | Runtime contract | YES — AEE-4 Part A freeze |
| `docs/aee/AEE5_API_REFERENCE.md` | API reference | YES — AEE-5 frozen |
| `docs/aee/AEE5_CONFIGURATION.md` | Configuration | YES — AEE-5 frozen |
| `docs/aee/AEE5_MIGRATION_GUIDE.md` | Migration | YES — AEE-5 frozen |
| `docs/aee/AEE5_RUNTIME_REGISTRY_ARCHITECTURE.md` | Architecture | YES — AEE-5 frozen |
| `docs/aee/AEE5_TEST_REPORT.md` | Test report | YES — AEE-5 frozen |
| `docs/aee/bootstrap/README.md` | Bootstrap docs index | YES — Phase 7 / Phase D |
| `docs/aee/bootstrap/operator-guide.md` | Operator guide | YES — quick start, profiles, CLI, release channels |
| `docs/aee/bootstrap/troubleshooting.md` | Troubleshooting | YES — common issues, diagnostics, recovery |
| `docs/aee/bootstrap/offline-bundle.md` | Offline/air-gapped | YES — bundle build/use |
| `gpt/GPT_SETUP_GUIDE.md` | GPT Action setup | YES — Custom GPT Action wiring |
| `aee-runtime/README.md` | Lightweight runtime | YES — AEE-4 Part B |
| `.pytest_cache/README.md` | Auto-generated | NO — pytest artifact, not relevant |

### 4.3 Phase / Design Reports (in `docs/`)

| Path | Category |
|------|----------|
| `docs/AEE0_BASELINE_REPORT.md` | Baseline hardening |
| `docs/AEE1_CORE_ADAPTER_REPORT.md` | Core adapter |
| `docs/AEE2_WORKER_CLAIM_PROTOCOL_REPORT.md` | Worker claim protocol |
| `docs/AEE3_CAPABILITY_MATCHING_REPORT.md` | Capability matching |
| `docs/AEE4_FINAL_VALIDATION_REPORT.md` | AEE-4 validation |
| `docs/Hermes_M2_Phase1_Strengthening_Design.md` | Phase 1 design |
| `docs/Hermes_M2_Phase1_Strengthening_Completion_Report.md` | Phase 1 completion |
| `docs/Hermes_M2_Phase2_Strengthening_Completion_Report.md` | Phase 2 completion |
| `docs/Hermes_M2_Phase2_Phase3_Roadmap.md` | Phase 2/3 roadmap |
| `docs/Hermes_M2_Phase3_Allowlist_Loosening_Report.md` | Phase 3 allowlist |
| `docs/Hermes_M2_Phase4_Delivery_Verification_Report.md` | Phase 4 delivery |
| `docs/Hermes_M2_Phase4_1_Intent_Mismatch_Report.md` | Phase 4.1 intent mismatch |
| `docs/phase1_acceptance_20260707_092245.md` | Phase 1 acceptance |
| `docs/phase1_acceptance_20260710_101906.md` | Phase 1 acceptance |

### 4.4 Configuration / Source-of-Truth Files

| Path | Category | README-Relevant? |
|------|----------|------------------|
| `aee/__init__.py` | Version (`__version__ = "2.0.0-rc1"`) | YES — version |
| `aee/profiles/descriptor.py` | `KNOWN_PROFILES`, `DEFAULT_PROFILE` | YES — profile matrix SoT |
| `aee/cli.py` | CLI UX (`--profile` flag) | YES — CLI reference |
| `openapi.yaml` | OpenAPI 3.1.0 schema (version 1.2.0) | YES — GPT Action schema |
| `.env.example` | Environment variable template | YES — config model |
| `Dockerfile` | Docker image build | YES — Docker usage |
| `docker-compose.yml` | Compose profiles | YES — Docker Compose usage |
| `docker-entrypoint.sh` | Profile selection at runtime | YES — Docker entrypoint |
| `install.sh` | Installer shell wrapper | YES — installation method |
| `install.ps1` | Windows installer | YES — Windows installation |
| `supervisor/hermes-runtime-bridge.conf` | Supervisord unit | YES — production run |
| `systemd/hermes-runtime-bridge.service` | Reference systemd unit | YES — non-Abacus production |
| `.github/workflows/ci-matrix.yml` | CI/CD matrix workflow | YES — CI/CD reference |
| `aee/ci/matrix.py` | CI matrix logic | YES — CI/CD reference |
| `requirements.txt` / `requirements.in` / `requirements.lock` | Dependencies | YES — install deps |
| `requirements-dev.in` / `requirements-dev.lock` | Dev dependencies | YES — dev install |
| `constraints.txt` | Version upper bounds | YES — dependency policy |
| `scripts/compile-deps.sh` | Dependency compilation | YES — dev workflow |
| `scripts/verify-deps.sh` | Dependency verification | YES — dev workflow |
| `.gitignore` / `.dockerignore` / `.tarignore` | Ignore rules | YES — packaging safety |
| `host.capabilities.yaml` | Host capability declaration | YES — deployment |
| `aee/deploy/adapters/*.py` | Deployment adapters | YES — provider-neutral deploy |
| `aee/deploy/samples/host.capabilities.*.yaml` | Host capability templates | YES — deployment samples |
| `aee/release/__init__.py` | Release strategy | YES — release |
| `aee/release/deprecation.py` | Deprecation plan | YES — AEE-MINI deprecation |

### 4.5 Master Plan (External, Authoritative)

| Path | Size | Status |
|------|------|--------|
| `/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md` | 457.3 KB | EXISTS — canonical architecture reference, ADR-009, §21.x chapter |

### 4.6 Test Infrastructure

| Path | Category |
|------|----------|
| `tests/test_safety.py` | Python blocklist unit test |
| `tests/test_health.sh` / `test_create_run.sh` / `test_get_run.sh` / `test_stop_run.sh` / `test_unsafe.sh` | Shell integration tests |
| `tests/acceptance/bootstrap_v1_acceptance.py` | Bootstrap v1 acceptance gate |
| `tests/e2e/{ubuntu,debian,macos}.sh` + `windows.ps1` | E2E platform tests |
| `aee/tests/test_aee9*.py` (72 files) | Epic 9.x targeted tests |

### 4.7 Bootstrap Infrastructure

| Path | Category |
|------|----------|
| `bootstrap/lib/` | Bootstrap library |
| `bootstrap/manifests/apt.deps.txt` / `brew.deps.txt` / `pwsh.deps.txt` | Platform dependency manifests |

---

## 5. Project Identity Determination

| Question | Answer | Evidence Source |
|----------|--------|-----------------|
| Project name | **AEE Runtime Bridge** | `README.md` line 1, `aee/__init__.py` |
| Version | `2.0.0-rc1` | `aee/__init__.py::__version__` |
| OpenAPI schema version | `1.2.0` | `openapi.yaml::info.version` |
| Product identity | Unified product — single entry point for AEE Runtime across four profiles | `README.md` lines 4-6, ADR-009 |
| Upstream | Hermes M2 (Abacus.ai) `/v1/runs` API at `127.0.0.1:8642` | `README.md` lines 14-16 |
| Bridge bind | `127.0.0.1:8787` | `.env.example`, `supervisor/hermes-runtime-bridge.conf` |
| Master Plan reference | `/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md` | `README.md` line 280 |

---

## 6. Supported Installation Methods

| Method | Command | Evidence |
|--------|---------|----------|
| Local foreground (uv) | `uv venv .venv && uv pip install -r requirements.txt && uvicorn app:app` | `README.md` §"Local run" |
| Production (supervisord, Abacus) | `sudo cp supervisor/*.conf && supervisorctl reread/update/start` | `README.md` §"Production run", `supervisor/hermes-runtime-bridge.conf` |
| Reference (systemd, non-Abacus) | `sudo cp systemd/*.service && systemctl enable --now` | `systemd/hermes-runtime-bridge.service` |
| Docker (single image, profile at run time) | `docker run aee:2.0.0-rc1 --profile {full,mini,edge,developer}` | `Dockerfile`, `docker-entrypoint.sh`, `README.md` §21.5 |
| Docker Compose (one service per profile) | `docker compose --profile {full,mini,edge,developer} up` | `docker-compose.yml` |
| Installer (POSIX) | `install.sh --profile {full,mini,edge,developer}` | `install.sh`, `README.md` §21.3 |
| Installer (Windows) | `install.ps1 -Profile {full,mini,edge,developer}` | `install.ps1` |
| Cloudflare Tunnel (public exposure) | `cloudflared tunnel route dns` + config | `README.md` §"Cloudflare Tunnel" |

---

## 7. Runtime Modes (Profiles)

| Profile | Dispatch | Cron | Subagent | Pipelines | Graph | Observability | DB Writes | Production DB |
|---------|----------|------|----------|-----------|-------|---------------|-----------|---------------|
| `full` | YES | YES | YES | YES | YES | YES | YES | YES |
| `mini` | YES | NO | NO | NO | subset | subset | dispatch only | YES |
| `edge` | NO (read-only) | NO | NO | NO | read-only | read-only | NO (`PRAGMA query_only=1`) | read-only |
| `developer` | YES (sandbox) | NO | YES (sandbox) | NO | sandbox | sandbox | tempdir only | NO |

**Source of truth:** `aee/profiles/descriptor.py::KNOWN_PROFILES = ("full", "mini", "edge", "developer")`, `DEFAULT_PROFILE = "full"`.

---

## 8. Testing Commands

| Suite | Command | Evidence |
|-------|---------|----------|
| Safety unit test | `PYTHONPATH=. ./.venv/bin/python tests/test_safety.py` | `README.md` §"Safety guard" |
| Safety shell test | `tests/test_unsafe.sh` | `README.md` §"Test scripts" |
| Health/create/get/stop shell | `tests/test_health.sh` etc. | `README.md` §"Test scripts" |
| Single AEE slice | `PYTHONPATH=. ./.venv/bin/python -m unittest aee.tests.test_aee99_*` | `README.md` §"AEE targeted tests" |
| Epic 9 regression sweep | `PYTHONPATH=. ./.venv/bin/python -m unittest discover -s aee/tests -p "test_aee9*.py"` | `README.md` §"AEE targeted tests" |
| Bootstrap v1 acceptance | `tests/acceptance/bootstrap_v1_acceptance.py` | `docs/aee/bootstrap/README.md` |
| E2E platform | `tests/e2e/{ubuntu,debian,macos}.sh`, `tests/e2e/windows.ps1` | `tests/e2e/` directory |
| Legacy tests/ suite | `pytest tests/` (implied by `requirements-dev.in`) | `requirements-dev.in` includes pytest |

---

## 9. CI/CD References

| Item | Path | Status |
|------|------|--------|
| CI/CD matrix workflow | `.github/workflows/ci-matrix.yml` | EXISTS — 4-profile matrix (`full,mini,edge,developer`), dry-run-first install, no production deploy |
| CI matrix logic | `aee/ci/matrix.py` | EXISTS — 18.9 KB |
| CI/CD readiness verification report | `reports/aee_ci_cd_readiness_verification.md` | EXISTS — untracked |
| CI/CD fix report | `reports/aee_cicd_fix_1.md` | EXISTS — untracked |
| CI/CD fix commit | `reports/aee_cicd_fix_1_commit.md` | EXISTS — untracked |
| Branch fix commit | HEAD `a9559a5 fix(ci): target main branch workflows` | TRACKED |

---

## 10. Repository Layout

```
~/hermes-runtime-bridge/
├── app.py                          # FastAPI app (136.8 KB)
├── openapi.yaml                    # OpenAPI 3.1.0 schema (37.4 KB)
├── requirements.txt / .in / .lock  # Dependencies
├── requirements-dev.in / .lock     # Dev dependencies
├── constraints.txt                 # Version upper bounds
├── .env.example                    # Environment template
├── Dockerfile                      # Single image, profile at run time
├── docker-compose.yml              # One service per profile
├── docker-entrypoint.sh            # Profile selection wrapper
├── install.sh / install.ps1        # Installer (POSIX / Windows)
├── host.capabilities.yaml          # Canonical host capability doc
├── aee/                            # Unified AEE package (v2.0.0-rc1)
│   ├── __init__.py                 # __version__
│   ├── cli.py                      # --profile, --version
│   ├── profiles/descriptor.py      # KNOWN_PROFILES, DEFAULT_PROFILE (SoT)
│   ├── adapters/                   # RuntimeAdapter + Hermes adapter
│   ├── api/                        # FastAPI routers
│   ├── artifacts/                  # Policy / collect
│   ├── audit/                      # Audit
│   ├── ci/                         # CI matrix logic
│   ├── config/                     # Config
│   ├── core/                       # Job models, state machine, registry
│   ├── deploy/                     # Provider-neutral deployment adapters
│   ├── dispatch/                   # Dispatch
│   ├── installer/                  # Installer backend
│   ├── observability/              # Event SOT
│   ├── observability_runtime/      # Emitter runtime
│   ├── operations/                 # Operations
│   ├── orchestrator/               # Provider Protocol + orchestrator
│   ├── platform/                   # Platform adapters
│   ├── profiles/                   # Profile descriptors
│   ├── release/                    # Release strategy + deprecation
│   ├── reporting/                  # Reporting
│   ├── runtimes/                   # Runtime descriptors
│   ├── security/                   # Safety/policy
│   ├── storage/                    # SQLite + future stores
│   └── tests/                      # 72 Epic 9.x targeted test files
├── aee-runtime/                    # AEE-4 lightweight agent runtime (Node.js)
├── bootstrap/                      # Bootstrap v1 library + manifests
├── cli/                            # CLI helpers (hermes-task)
├── config/                         # Config
├── data/                           # Runtime DB (dispatcher.db, WAL, SHM)
├── dispatcher/                     # Dispatcher (db, manager, models)
├── docs/                           # Documentation (AEE phase reports, guides)
├── gpt/                            # GPT Action setup guide + examples
├── logs/                           # Logs (supervisord, shadow_run)
├── prompts/                        # Prompt templates
├── reports/                        # Per-task reports + work-order reports
├── research/                       # Research modules
├── runtime_data/                   # Local-only runtime data
├── scripts/                        # compile-deps.sh, verify-deps.sh
├── supervisor/                     # supervisord unit
├── systemd/                        # reference systemd unit
├── tests/                          # Legacy tests + acceptance + e2e
├── .github/workflows/              # CI/CD (ci-matrix.yml)
├── .gitignore / .dockerignore / .tarignore
└── README.md
```

---

## 11. Configuration Model

| Layer | Source | Evidence |
|-------|--------|----------|
| Environment variables | `.env` (from `.env.example`) | `.env.example` — 5 bridge keys, HERMES_BASE_URL, HERMES_API_KEY, BRIDGE_HOST/PORT |
| Multi-key auth | `BRIDGE_API_KEY`, `GPT_BRIDGE_API_KEY`, `CLAUDE_BRIDGE_API_KEY`, `CURSOR_BRIDGE_API_KEY`, `MCP_BRIDGE_API_KEY` | `.env.example` lines 14-23 |
| Profile selection | `--profile` flag (CLI/installer/Docker) or `profile` field on `POST /runs` | `README.md` §21.2/21.4, `aee/cli.py` |
| Docker profile env | `AEE_PROFILE`, `AEE_DB_READ_ONLY` (edge), `AEE_DB_PATH` (developer) | `docker-entrypoint.sh` |
| Host capabilities | `host.capabilities.yaml` | `aee/deploy/` |
| Runtime registry config | `AEE_RUNTIME_CONFIG` env var (YAML) | `docs/aee/AEE5_CONFIGURATION.md` |
| Dependency locking | `requirements.lock` (Linux), `requirements.lock.darwin` (macOS) | `requirements.lock`, `requirements.lock.darwin` |
| Version bounds | `constraints.txt` | `constraints.txt` |

---

## 12. Troubleshooting Topics (Available Evidence)

| Topic | Evidence Source |
|-------|----------------|
| Profile/channel invalid (exit 3) | `docs/aee/bootstrap/troubleshooting.md` §1.0 |
| Common install issues | `docs/aee/bootstrap/troubleshooting.md` §1 |
| Platform-specific issues | `docs/aee/bootstrap/troubleshooting.md` (platform section) |
| Diagnostics / recovery | `docs/aee/bootstrap/troubleshooting.md` |
| Supervisord vs systemd | `README.md` §"Production run", `supervisor/*.conf` |
| Cloudflare Tunnel setup | `README.md` §"Cloudflare Tunnel" |
| Safety guard / dangerous patterns | `README.md` §"Safety guard", `tests/test_safety.py` |
| DO NOT pack runtime data | `README.md` §"DO NOT pack runtime data", `.tarignore` |
| Offline / air-gapped | `docs/aee/bootstrap/offline-bundle.md` |
| Migration from AEE-MINI | `docs/MIGRATION_FROM_AEE_MINI.md`, `README.md` §"Migration" |

---

## 13. Recommended README Outline — Evidence-Backed Section Mapping

The existing `README.md` is already comprehensive (291 lines, 17 sections). Below is the **recommended outline for a refreshed/consolidated README**, with each section mapped to its authoritative evidence source and a "Document Now vs Defer" classification.

| # | Proposed Section | Evidence Source | Status |
|---|-----------------|-----------------|--------|
| 1 | Title + one-line description + architecture diagram | `README.md` lines 1-16, `aee/__init__.py` | **NOW** — already written |
| 2 | Why this exists (3-point value prop) | `README.md` lines 18-30 | **NOW** — already written |
| 3 | Product profiles matrix (§21.1) | `aee/profiles/descriptor.py::KNOWN_PROFILES`, `README.md` §"Product Surface" | **NOW** — already written, code-backed |
| 4 | Selecting a profile (CLI / installer / Docker / POST /runs) | `aee/cli.py`, `install.sh`, `docker-entrypoint.sh`, `README.md` §"Selecting a profile" | **NOW** — already written |
| 5 | Installation | `README.md` §"Local run" + §"Production run", `install.sh`, `install.ps1`, `Dockerfile`, `docker-compose.yml` | **NOW** — already written, but see §14 gaps |
| 6 | Docker Compose usage | `docker-compose.yml` (80-line header with usage + resource floors) | **NOW** — evidence exists; README currently lacks a dedicated Docker Compose section |
| 7 | Endpoints table | `README.md` §"Endpoints", `openapi.yaml` | **NOW** — already written |
| 8 | Configuration model (.env, multi-key auth, profile env) | `.env.example`, `docker-entrypoint.sh`, `README.md` (scattered) | **NOW** — evidence exists; README could consolidate |
| 9 | Safety guard | `README.md` §"Safety guard", `app.py::DANGEROUS_PATTERNS`, `tests/test_safety.py` | **NOW** — already written |
| 10 | Testing | `README.md` §"AEE targeted tests" + §"Test scripts", `tests/`, `aee/tests/`, `tests/acceptance/`, `tests/e2e/` | **NOW** — already written; could add acceptance + e2e |
| 11 | CI/CD | `.github/workflows/ci-matrix.yml`, `aee/ci/matrix.py` | **NOW** — evidence exists; README currently lacks a CI/CD section |
| 12 | Repository layout | `README.md` §"Layout" | **NOW** — already written; could be refreshed |
| 13 | Adapter contract | `docs/HERMES_ADAPTER_CONTRACT_MATRIX.md` | **NOW** — already referenced |
| 14 | GPT Action setup | `gpt/GPT_SETUP_GUIDE.md` | **NOW** — evidence exists; README could add a pointer |
| 15 | Bootstrap / operator docs | `docs/aee/bootstrap/README.md` + operator-guide + troubleshooting + offline-bundle | **NOW** — evidence exists; README could add a pointer |
| 16 | Cloudflare Tunnel | `README.md` §"Cloudflare Tunnel" | **NOW** — already written |
| 17 | Troubleshooting | `docs/aee/bootstrap/troubleshooting.md`, `README.md` (scattered) | **NOW** — evidence exists; README could add a pointer section |
| 18 | DO NOT pack runtime data | `README.md` §"DO NOT pack", `.tarignore`, `.dockerignore`, `.gitignore` | **NOW** — already written |
| 19 | Migration from AEE-MINI | `docs/MIGRATION_FROM_AEE_MINI.md`, `README.md` §"Migration" | **NOW** — already written |
| 20 | Authoritative references (§21.x table) | `README.md` §"Authoritative references", Master Plan | **NOW** — already written |
| 21 | What's intentionally not in v1 | `README.md` §"What's intentionally not in v1" | **NOW** — already written; may need update for v2.0.0-rc1 |
| 22 | Release channels / version pinning / drift detection | `docs/aee/bootstrap/operator-guide.md`, `aee/release/` | **DEFER** — evidence is in operator-guide, but README-level summary needs verification of current channel state |
| 23 | Provider-neutral deployment (§21.6) | `aee/deploy/adapters/`, `host.capabilities.yaml`, `aee/deploy/samples/` | **DEFER** — evidence exists but README-level summary needs verification of which adapters are live vs stub |
| 24 | Observability / audit events | `aee/observability/`, `aee/observability_runtime/` | **DEFER** — evidence exists but these are untracked working-tree residue (AEE-7.4 slices, not committed); README should not document uncommitted features |
| 25 | AEE-5 Runtime Registry | `docs/aee/AEE5_*.md` | **DEFER** — AEE-5 frozen docs exist but the README is the unified product entry point; deep AEE-5 registry detail belongs in `docs/aee/` not README top-level |
| 26 | Bootstrap v1 full spec | `reports/aee_bootstrap_v1_spec.md` | **DEFER** — spec is a planning artifact (untracked); README should point to operator-guide not spec |

---

## 14. Inconsistencies, Duplicate Documentation, Stale References, Missing Documentation

### 14.1 Inconsistencies

| ID | Finding | Evidence |
|----|---------|----------|
| INC-1 | README "What's intentionally not in v1" lists "Per-run audit SQLite (planned for v1.2)" but `dispatcher/db.py` + `data/dispatcher.db` (1.7 MB) already exist and are actively used | `README.md` line 235 vs `data/dispatcher.db` 1.7M + WAL 3.9M |
| INC-2 | README "What's intentionally not in v1" lists "SSE event tailing (planned for v3)" but AEE-7.4 observability emitter infrastructure exists (untracked) | `README.md` line 236 vs `aee/observability_runtime/` |
| INC-3 | OpenAPI schema version is `1.2.0` but AEE package version is `2.0.0-rc1` — version drift between schema and product | `openapi.yaml::info.version` vs `aee/__init__.py::__version__` |
| INC-4 | README layout shows `aee/tests/test_aee9*.py` but actual count is 72 test files spanning `test_aee81_*` through `test_aee99_*` | `README.md` line 214 vs `ls aee/tests/*.py \| wc -l` = 72 |
| INC-5 | Dockerfile image tag comment says `aee:2.0.0-rc1.gamma` but README says `aee:2.0.0` | `Dockerfile` line 21 vs `README.md` line 71 |

### 14.2 Duplicate Documentation

| ID | Finding | Evidence |
|----|---------|----------|
| DUP-1 | Migration from AEE-MINI is documented in both `README.md` §"Migration" AND `docs/MIGRATION_FROM_AEE_MINI.md` | Two sources, same topic |
| DUP-2 | Adapter contract is described in `README.md` §"Adapter contract" (brief) AND `docs/HERMES_ADAPTER_CONTRACT_MATRIX.md` (full) | README correctly points to full doc — this is acceptable, not a true duplicate |
| DUP-3 | Profile matrix appears in `README.md` §"Product Surface" AND `docs/aee/bootstrap/operator-guide.md` | Two copies of the matrix; README notes "the code is the enforcement" but operator-guide has its own copy |
| DUP-4 | Installation instructions appear in `README.md` (local + supervisord) AND `docs/aee/bootstrap/operator-guide.md` (quick start) | Overlapping content |

### 14.3 Stale References

| ID | Finding | Evidence |
|----|---------|----------|
| STALE-1 | README "What's intentionally not in v1" section header uses "v1" but product is at `2.0.0-rc1` | `README.md` line 233 |
| STALE-2 | README layout tree shows `aee/tests/test_aee9*.py` as the only test pattern, but `test_aee81_*` through `test_aee99_*` all exist | `README.md` line 214 |
| STALE-3 | `reports/aee_current_remaining_work_20260730.md` is untracked — contains current-state audit but is not committed | `git status` shows untracked |

### 14.4 Missing Documentation (for README)

| ID | Finding | Evidence |
|----|---------|----------|
| MISS-1 | No dedicated Docker Compose section in README | `docker-compose.yml` has 80-line header with usage but README only mentions `docker run` |
| MISS-2 | No CI/CD section in README | `.github/workflows/ci-matrix.yml` exists (10.8 KB) but README has no CI/CD reference |
| MISS-3 | No GPT Action setup pointer in README | `gpt/GPT_SETUP_GUIDE.md` exists (465+ lines) but README does not link to it |
| MISS-4 | No bootstrap operator docs pointer in README | `docs/aee/bootstrap/` has 4 docs but README does not link to them |
| MISS-5 | No troubleshooting pointer section in README | `docs/aee/bootstrap/troubleshooting.md` exists (304+ lines) but README has no troubleshooting section |
| MISS-6 | No dependency management section in README | `requirements.in/.lock`, `constraints.txt`, `scripts/compile-deps.sh`, `scripts/verify-deps.sh` exist but README does not document the lockfile workflow |
| MISS-7 | No Windows installer mention in README | `install.ps1` exists (9.0 KB) but README only mentions `install.sh` |
| MISS-8 | No acceptance gate / e2e test mention in README | `tests/acceptance/bootstrap_v1_acceptance.py`, `tests/e2e/` exist but README only documents `tests/` shell scripts + `aee/tests/` |
| MISS-9 | No host capabilities / deployment adapter section in README | `host.capabilities.yaml`, `aee/deploy/adapters/` exist but README does not document them |

---

## 15. Evidence Gaps

| Gap ID | Description | Impact |
|--------|-------------|--------|
| GAP-1 | AEE-7.4 observability slices (`aee/observability/`, `aee/observability_runtime/`) are **untracked working-tree residue** — not committed. README should NOT document these as shipped features. | Cannot document observability in README until committed |
| GAP-2 | `reports/aee_current_remaining_work_20260730.md` is untracked — current-state audit is not part of git history | README cannot reference it as authoritative |
| GAP-3 | Docker image tag is inconsistent (`2.0.0-rc1` vs `2.0.0-rc1.gamma` vs `2.0.0`) — cannot recommend a single canonical tag without operator confirmation | README Docker section needs tag reconciliation |
| GAP-4 | Provider-neutral deployment adapters (`aee/deploy/adapters/`) exist for 7 platforms (abacus, docker, macbook, terraform_aws, windows, zo, base) but which are "live" vs "stub" is not verified from code in this read-only pass | README deployment section should defer adapter status detail |
| GAP-5 | Release channel / version pinning / drift detection is documented in operator-guide but the current live channel state (which channel is active, what ref is pinned) is not verified | README release section should defer to operator-guide |
| GAP-6 | OpenAPI version (1.2.0) vs AEE version (2.0.0-rc1) drift — unclear if OpenAPI schema has been updated for Epic 9 or still reflects Phase 1/2 | README endpoints section should note schema version may lag |

---

## 16. Recommended Minimal Implementation Work Order

The existing README is already strong (291 lines, 17 well-structured sections). The minimal work order to achieve "README readiness" is a **targeted refresh**, not a rewrite.

### Step 1: Fix stale/inconsistent content (evidence-backed, no new research needed)

1. Update "What's intentionally not in v1" section header → "Roadmap" or "What's not yet shipped" (STALE-1).
2. Update the "Per-run audit SQLite" line → note that `dispatcher.db` is now live (INC-1).
3. Update Docker image tag to match Dockerfile comment or reconcile to a single canonical tag (INC-5, GAP-3).
4. Update layout tree to show `test_aee81_*` … `test_aee99_*` range instead of just `test_aee9*` (INC-4, STALE-2).
5. Note OpenAPI schema version vs AEE version drift (INC-3, GAP-6).

### Step 2: Add missing high-value sections (evidence exists on disk)

6. Add a **Docker Compose** section after "Production run" — content from `docker-compose.yml` header (usage, resource floors, named volumes) (MISS-1).
7. Add a **CI/CD** section after "Test scripts" — content from `.github/workflows/ci-matrix.yml` header (4-profile matrix, dry-run-first, no production deploy) (MISS-2).
8. Add a **GPT Action Setup** pointer line — link to `gpt/GPT_SETUP_GUIDE.md` (MISS-3).
9. Add a **Bootstrap Operator Docs** pointer line — link to `docs/aee/bootstrap/` (MISS-4).
10. Add a **Troubleshooting** pointer section — link to `docs/aee/bootstrap/troubleshooting.md` (MISS-5).
11. Add a **Dependency Management** subsection under installation — mention `requirements.in` → `requirements.lock` workflow + `constraints.txt` + `scripts/compile-deps.sh` (MISS-6).
12. Add Windows installer mention (`install.ps1`) alongside `install.sh` (MISS-7).
13. Add acceptance gate + e2e test mention under "Testing" (MISS-8).

### Step 3: Defer sections that lack verified evidence

14. **DO NOT** add observability/audit events section (GAP-1 — untracked).
15. **DO NOT** add provider-neutral deployment adapter detail section (GAP-4 — adapter live/stub status unverified).
16. **DO NOT** add release channel current-state section (GAP-5 — live channel state unverified).
17. **DO NOT** reference `reports/aee_current_remaining_work_20260730.md` as authoritative (GAP-2 — untracked).

### Step 4: Consolidate duplicates (optional, low-risk)

18. Consider trimming `README.md` §"Migration from AEE-MINI" to a 3-line pointer + link to `docs/MIGRATION_FROM_AEE_MINI.md` (DUP-1).
19. Consider trimming `README.md` profile matrix to a 2-line summary + link to `docs/aee/bootstrap/operator-guide.md` for the full matrix (DUP-3) — **OR** keep the matrix in README (it is code-backed and the README is the operator entry point). Recommendation: **keep the matrix in README** (it is the authoritative operator-facing copy, code is the enforcement).

### Step 5: Verify and commit

20. Run `PYTHONPATH=. ./.venv/bin/python tests/test_safety.py` to confirm safety guard still works.
21. Run `PYTHONPATH=. ./.venv/bin/python -m unittest discover -s aee/tests -p "test_aee9*.py"` to confirm no regression.
22. Commit README.md as a single atomic commit: `docs(readme): refresh README for 2.0.0-rc1 readiness`.

---

## 17. Summary Assessment

| Dimension | Status |
|-----------|--------|
| Existing README quality | HIGH — 291 lines, 17 sections, code-backed profile matrix, install/run/test/safety all covered |
| Existing documentation depth | HIGH — 20+ docs in `docs/`, 4 bootstrap operator docs, GPT setup guide, adapter contract matrix, migration guide |
| Stale/inconsistent content | 5 items identified (§14.1) — all fixable with targeted edits |
| Missing README sections | 9 items identified (§14.4) — 7 have evidence on disk, 2 need deferral |
| Evidence gaps | 6 items identified (§15) — all relate to untracked code or unverified live state |
| Duplicate documentation | 4 items identified (§14.2) — 2 are acceptable (README points to full doc), 2 are trimmable |
| Shadow-run interference | NONE — all operations were read-only |
| Implementation work order | 5 steps, ~13 targeted edits, 0 new research needed for Steps 1-2 |

---

## 18. Verdict

**PASS**

A complete evidence-backed README inventory and implementation plan has been produced. The artifact has been written and verified. All six objectives were met:

1. ✅ Inventoried all existing documentation (§4 — 20+ docs, 14 config/SoT files, Master Plan, test infrastructure).
2. ✅ Determined authoritative project identity, installation methods, runtime modes, Docker Compose usage, testing commands, CI/CD references, repository layout, configuration model, and troubleshooting topics (§5-12).
3. ✅ Identified sections documentable now (20 sections) vs deferred (6 sections) (§13).
4. ✅ Produced recommended README outline with evidence-backed content sources (§13).
5. ✅ Identified 5 inconsistencies, 4 duplicates, 3 stale references, 9 missing docs (§14).
6. ✅ Recommended single minimal implementation work order — 5 steps, ~13 edits (§16).

Shadow-run non-interference confirmed (§3). Read-only mode maintained throughout.

---

_End of report._