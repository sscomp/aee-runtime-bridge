# AEE Runtime Bridge

A thin, safe OpenAI-Custom-GPT-Action / MCP-friendly facade in front of the
Hermes M2 (Abacus.ai) `/v1/runs` API. As of Epic 9 (ADR-009), this repository
is the **unified product** — the single entry point for the AEE Runtime
across all four product profiles (`full`, `mini`, `edge`, `developer`).

- **Product version:** `2.0.0-rc1` (source: `aee/__init__.py::__version__`)
- **OpenAPI schema version:** `1.2.0` (source: `openapi.yaml::info.version`)

> Note: the OpenAPI schema version (`1.2.0`) is managed separately from the
> AEE product version (`2.0.0-rc1`) and may lag behind the product version.
> Both are bumped intentionally; do not assume they are in lock-step.

```
ChatGPT Custom GPT Action          (or MCP client)
        │  Authorization: Bearer ***
        ▼
AEE Runtime Bridge   (this service, 127.0.0.1:8787)
        │  Authorization: Bearer ***   (different key)
        ▼
Hermes M2 API server    (127.0.0.1:8642, not exposed to internet)
```

## Why this exists

Hermes M2 (Abacus.ai) already exposes a full OpenAI-compatible API on
`127.0.0.1:8642`, including a `POST /v1/runs` → `GET /v1/runs/{id}` async
run protocol and SSE event streaming. The bridge adds three things the
upstream API doesn't provide by itself:

1. **Stable OpenAPI surface** for ChatGPT Custom GPT Actions and MCP tools.
2. **Independent bearer key** so the API key sitting in ChatGPT cannot drive
   arbitrary upstream endpoints, only the 4 allowlisted ones.
3. **First-line safety**: rejects `input` containing destructive patterns
   (`rm -rf /`, `cat ~/.hermes/.env`, `export API_SERVER_KEY`, etc.) before
   the request ever reaches Hermes.

## Product Surface — the four profiles (§21.1)

This repository is the unified product. There is one product, one
codebase, one Docker image, and four **profiles** that select the runtime
capability set. The profile is selected per invocation via the `--profile`
flag (CLI, installer, `docker run`) or the `profile` field on `POST /runs`.

The canonical matrix is defined in code at
`aee/profiles/descriptor.py::KNOWN_PROFILES`
(`KNOWN_PROFILES = ("full", "mini", "edge", "developer")`). The matrix
below is the documentation of that tuple; the code is the enforcement.

| Capability | `full` | `mini` | `edge` | `developer` |
|---|---|---|---|---|
| Dispatch (`POST /runs`) | ✅ | ✅ | ❌ (read-only) | ✅ (sandbox) |
| Cron creation | ✅ | ❌ | ❌ | ❌ (sandbox only) |
| Subagent delegation | ✅ | ❌ | ❌ | ✅ (sandbox) |
| Long-running pipelines | ✅ | ❌ | ❌ | ❌ |
| Graph queries | ✅ | ✅ (subset) | ✅ (read-only) | ✅ (sandbox) |
| Observability events | ✅ | ✅ (subset) | ✅ (read-only) | ✅ (sandbox) |
| DB writes | ✅ | ✅ (dispatch only) | ❌ (`PRAGMA query_only=1`) | ✅ (tempdir only) |
| Production DB access | ✅ | ✅ | ✅ (read-only) | ❌ |
| Toolset | full | `terminal`, `file`, `web` subset | `file` (read), `web` (read) | full (sandbox) |

The profile order `(full, mini, edge, developer)` is invariant; downstream
sections preserve this order in their CLI flags, installer arguments, and
Docker profile args.

## Selecting a profile (§21.2, §21.3, §21.5)

Three selection surfaces, one source of truth:

- **CLI**: `aee --profile {full,mini,edge,developer} <subcommand>`.
  Default: `full` (matches `DEFAULT_PROFILE` in `descriptor.py`).
  `--profile bogus` → `UnknownProfileError`, not a silent fallback.
- **Installer**: `install.sh --profile {full,mini,edge,developer}`
  (POSIX) or `install.ps1 -Profile {full,mini,edge,developer}` (Windows).
  Single installer absorbs AEE-MINI's hardening (idempotent pre-flight, `aee`
  system user, `0600` env file, smoke test). Profile switch on an existing
  install is **rejected** ("profile change requires uninstall + reinstall").
- **Docker**: `docker run aee:2.0.0-rc1 --profile {full,mini,edge,developer}`.
  One image, one codebase, profile selected at `docker run` time.
  `--profile edge` sets `AEE_DB_READ_ONLY=1`.
  `--profile developer` → tempdir DB + smoke test + interactive shell.

The AEE-MINI standalone installer continues to work for existing B2
deployments during the §21.10 deprecation window — but new deployments
should use the unified installer with `--profile mini`.

## Adapter contract

The Hermes adapter contract — what the adapter sends and accepts, with
evidence labels (`VERIFIED_FROM_CODE`, `VERIFIED_FROM_TEST_STUB`,
`ASSUMED`, `UNKNOWN`) — is documented in
[`docs/HERMES_ADAPTER_CONTRACT_MATRIX.md`](docs/HERMES_ADAPTER_CONTRACT_MATRIX.md).
That matrix was **moved** (not copied) from AEE-MINI per §21.9 and is now
the canonical copy; the AEE-MINI frozen archive copy stays on disk
untouched as the archive reference.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET  | `/health`                    | no  | Liveness + Hermes reachability |
| POST | `/runs`                      | yes | Start a new run; returns `run_id` |
| GET  | `/runs/{run_id}`             | yes | Poll status / final output |
| GET  | `/runs/{run_id}/summary`     | yes | ChatGPT-friendly summary |
| POST | `/runs/{run_id}/stop`        | yes | Cancel a running task |

The schema for `POST /runs` is in `openapi.yaml` (paste this into the
Custom GPT Action schema box). For setting up a ChatGPT Custom GPT Action
against this bridge, see [`gpt/GPT_SETUP_GUIDE.md`](gpt/GPT_SETUP_GUIDE.md).

## Configuration model

Configuration is layered. The authoritative template is
[`.env.example`](.env.example).

| Layer | Source | Notes |
|---|---|---|
| Bridge bind | `BRIDGE_HOST`, `BRIDGE_PORT` (default `127.0.0.1:8787`) | `.env.example` |
| Client keys | `BRIDGE_API_KEY`, `GPT_BRIDGE_API_KEY`, `CLAUDE_BRIDGE_API_KEY`, `CURSOR_BRIDGE_API_KEY`, `MCP_BRIDGE_API_KEY` | Bridge accepts ANY of the 5; rotate one without touching the others. Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| Upstream Hermes | `HERMES_BASE_URL` (default `http://127.0.0.1:8642`), `HERMES_API_KEY` | Bridge → Hermes M2 gateway |
| Profile selection | `--profile` flag (CLI/installer/Docker) or `profile` field on `POST /runs` | `aee/cli.py`, `app.py::POST /runs` |
| Docker profile env | `AEE_PROFILE`, `AEE_DB_READ_ONLY` (edge), `AEE_DB_PATH` (developer) | `docker-entrypoint.sh` |
| Host capabilities | `host.capabilities.yaml` | Canonical for M2 host; templates in `aee/deploy/samples/` |
| Dependency locking | `requirements.lock` (Linux), `requirements.lock.darwin` (macOS) | See "Dependency management" below |
| Version upper bounds | `constraints.txt` | Editable without regenerating lockfiles |

Never bake secrets into the Docker image. `.env` is excluded by
`.dockerignore` and mounted at runtime.

## Installation

### Local run (foreground, uv)

```bash
cd ~/hermes-runtime-bridge
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env
# edit .env — fill in HERMES_API_KEY (and a new BRIDGE_API_KEY)
./.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8787
```

### Production run (Abacus.AI host — supervisord)

The Abacus host uses **supervisord (PID 7)**, not systemd. `systemctl
enable` will not work. Use the supervisord unit:

```bash
sudo cp supervisor/hermes-runtime-bridge.conf /etc/supervisor/conf.d/
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start hermes-runtime-bridge
sudo supervisorctl status hermes-runtime-bridge

curl -sS http://127.0.0.1:8787/health | python3 -m json.tool
```

Logs: `~/hermes-runtime-bridge/logs/bridge.{out,err}.log`.

> A reference systemd unit is provided in `systemd/` for use on a normal
> Linux box. Do NOT use it on Abacus.

### Installer (POSIX)

```bash
./install.sh --profile {full,mini,edge,developer}
```

The installer is idempotent: it runs a read-only pre-flight, creates an
`aee` system user, writes a `0600` env file, and runs a smoke test. A
profile switch on an existing install is rejected (uninstall + reinstall
required).

### Installer (Windows, experimental)

```powershell
.\install.ps1 -Profile {full,mini,edge,developer}
```

`install.ps1` is the Windows counterpart of `install.sh`. It targets
PowerShell 5.1+ and is shipped as part of Phase C (W7/W13).

### Dependency management

Runtime dependencies are hash-pinned via `uv pip compile`:

- `requirements.in` → `requirements.lock` (Linux)
- `requirements-dev.in` → `requirements-dev.lock` (dev)
- `requirements.lock.darwin` (macOS)
- `constraints.txt` — version upper bounds, editable without regenerating

Regenerate lockfiles:

```bash
./scripts/compile-deps.sh
```

Verify lockfile integrity (read-only):

```bash
./scripts/verify-deps.sh
```

### Cloudflare Tunnel

Add the bridge to `~/.cloudflared/config.yml`:

```yaml
ingress:
  - hostname: hermes-runtime.biaobecue.com
    service: http://127.0.0.1:8787
  # ... existing entries ...
  - service: http_status:404
```

Then create the DNS route (one-time, via Cloudflare dashboard or `cloudflared`):

```bash
cloudflared tunnel route dns abacus-claw hermes-runtime.biaobecue.com
```

And reload the tunnel. Verify:

```bash
curl -sS https://hermes-runtime.biaobecue.com/health
```

## Docker Compose (§21.5, §21.6.E)

`docker-compose.yml` defines **one service per profile**, all using the
single image built from `Dockerfile`. Profile is selected at run time via
the entrypoint's `--profile` flag.

```bash
docker compose --profile full up
docker compose --profile mini up
docker compose --profile edge up
docker compose --profile developer up
```

Required environment (provide via `.env` or `export`):

- `BRIDGE_API_KEY` — CLI/CI client key
- `GPT_BRIDGE_API_KEY` — ChatGPT Custom GPT Action key
- `HERMES_BASE_URL` — upstream Hermes gateway URL
- `HERMES_API_KEY` — upstream Hermes API key

Resource floors (per §21.6.C):

| Profile | CPU | Memory | Disk |
|---|---|---|---|
| `full` | 2 | 4096 MB | 2048 MB |
| `mini` | 1 | 1024 MB | 1024 MB |
| `edge` | 1 | 1024 MB | 512 MB |
| `developer` | 1 | 1024 MB | 512 MB |

Named volumes (`aee-data-full`, `aee-data-mini`, `aee-data-edge`)
persist the dispatcher SQLite DB across container restarts. The
`developer` profile does not use a persistent named volume — it runs
with a temporary database at `/tmp/aee-dev.db`. See the
`docker-compose.yml` header for the full reference.

## Safety guard

`POST /runs` rejects input matching any pattern in `app.py::DANGEROUS_PATTERNS`.
Run `PYTHONPATH=. ./.venv/bin/python tests/test_safety.py` to self-test.
The Python self-test covers 19 dangerous patterns and 9 clean phrases.

The curl version is in `tests/test_unsafe.sh`.

> This is a *first line of defence*, not a complete security model. Hermes'
> own toolset config and approval gates are the real enforcement layer.

## Testing

### Safety unit test

```bash
cd ~/hermes-runtime-bridge
PYTHONPATH=. ./.venv/bin/python tests/test_safety.py
```

### Shell integration tests

| Script | What it does |
|---|---|
| `tests/test_health.sh`            | Hit `/health` and confirm Hermes reachable. |
| `tests/test_create_run.sh`        | Start a run, save `run_id` to `/tmp/bridge_last_run_id`. |
| `tests/test_get_run.sh`           | Poll `/runs/{id}` and `/runs/{id}/summary`. |
| `tests/test_stop_run.sh`          | Request cancellation. |
| `tests/test_unsafe.sh`            | Send 16 dangerous payloads; expect 400 each. |

All scripts honour `BRIDGE_API_KEY` and an optional `BASE` arg.

### AEE targeted tests

The `aee/tests/` directory contains per-Epic-slice targeted test files
spanning `test_aee7_*` through `test_aee99_*` (Epic 7.x, 8.x, 9.x and
Phase 2–4 bootstrap tests). Run a single slice:

```bash
PYTHONPATH=. ./.venv/bin/python -m unittest aee.tests.test_aee99_documentation_migration -v
```

Run the full Epic 9 regression sweep:

```bash
PYTHONPATH=. ./.venv/bin/python -m unittest discover -s aee/tests -p "test_aee9*.py" -v
```

### Bootstrap v1 acceptance gate

The Bootstrap v1 acceptance gate (`tests/acceptance/bootstrap_v1_acceptance.py`)
is a stdlib-only, hermetic, read-only test that verifies the installer
interface is correct and the plan is deterministic. It makes no network
calls, spawns no subprocesses, and writes no files.

```bash
PYTHONPATH=. python3 -m unittest tests.acceptance.bootstrap_v1_acceptance -v
```

### E2E platform tests

E2E scripts in `tests/e2e/` exercise the installer on each supported
platform: `ubuntu.sh`, `debian.sh`, `macos.sh`, and `windows.ps1`.

## CI/CD (§21.7)

CI/CD is driven by a single GitHub Actions workflow:
[`.github/workflows/ci-matrix.yml`](.github/workflows/ci-matrix.yml).

- **Matrix:** one job per profile (`full`, `mini`, `edge`, `developer`),
  all four green on `main`.
- **Dry-run-first:** every job runs `install.sh --profile <p> --dry-run`
  before any `--execute` path.
- **No production deploy:** the workflow only tests; it never deploys,
  restarts, or mutates runtime state.
- **Single source of truth:** the four profile names and subset suites
  come from `aee/ci/matrix.py` (Python). The workflow's `include:` block
  mirrors that module; the `aee.tests.test_aee97_cicd_matrix` targeted
  tests verify the two agree.
- **Stdlib-only tests:** the AEE test suite uses `unittest`; no `pip
  install` is required for targeted tests.

`full` runs the complete AEE suite via `unittest discover`; `mini`,
`edge`, and `developer` run per-profile subset suites defined in
`aee/ci/matrix.py::SUBSET_SUITES_BY_PROFILE`.

## Bootstrap operator docs

Operator-facing documentation for AEE Bootstrap v1 lives in
[`docs/aee/bootstrap/`](docs/aee/bootstrap/):

| Document | Description |
|---|---|
| [operator-guide.md](docs/aee/bootstrap/operator-guide.md) | Quick start, profiles, CLI, release channels, version pinning, stage lifecycle, rollback |
| [troubleshooting.md](docs/aee/bootstrap/troubleshooting.md) | Common issues, platform-specific issues, diagnostics, recovery |
| [offline-bundle.md](docs/aee/bootstrap/offline-bundle.md) | Building and using offline/air-gapped bundles |
| [README.md](docs/aee/bootstrap/README.md) | Documentation index |

## Troubleshooting

For common issues (profile/channel invalid, pre-flight failures,
platform-specific issues, diagnostics, and recovery procedures), see
[`docs/aee/bootstrap/troubleshooting.md`](docs/aee/bootstrap/troubleshooting.md).

Quick pointers:

- **Profile or channel invalid (exit 3):** `aee install --help` to see
  valid `--profile` / `--channel` choices. Profile validation is
  case-sensitive; channel validation is case-insensitive.
- **Supervisord vs systemd:** on Abacus use `supervisorctl` (see
  "Production run" above). The `systemd/` unit is for non-Abacus Linux only.
- **Safety guard / dangerous patterns:** see "Safety guard" above and
  `tests/test_safety.py`.
- **DO NOT pack runtime data:** see below.

## Layout

```
~/hermes-runtime-bridge/
├── app.py                          # FastAPI app
├── openapi.yaml                    # Custom GPT Action schema (OpenAPI 3.1.0)
├── requirements.txt / .in / .lock  # Runtime deps (hash-pinned)
├── requirements-dev.in / .lock     # Dev deps
├── constraints.txt                 # Version upper bounds
├── .env.example                    # Environment template
├── Dockerfile                      # Single image, profile at run time
├── docker-compose.yml              # One service per profile
├── docker-entrypoint.sh            # Profile selection wrapper
├── install.sh / install.ps1        # Installer (POSIX / Windows)
├── host.capabilities.yaml          # Canonical host capability doc (M2)
├── aee/                            # unified AEE package (v2.0.0-rc1)
│   ├── __init__.py                 # __version__
│   ├── cli.py                      # --profile, --version
│   ├── profiles/descriptor.py      # KNOWN_PROFILES, DEFAULT_PROFILE (SoT)
│   ├── adapters/                   # RuntimeAdapter + Hermes adapter
│   ├── api/                        # FastAPI routers
│   ├── artifacts/                  # Policy / collect
│   ├── audit/                      # Audit
│   ├── ci/                         # CI matrix logic (§21.7)
│   ├── config/                     # Config
│   ├── core/                       # Job models, state machine, registry
│   ├── deploy/                     # Provider-neutral deployment adapters + samples
│   ├── dispatch/                   # Dispatch
│   ├── installer/                  # Installer backend (§21.3)
│   ├── observability/             # Event SOT (untracked, see "Roadmap")
│   ├── observability_runtime/      # Emitter runtime (untracked, see "Roadmap")
│   ├── operations/                 # Operations
│   ├── orchestrator/               # Provider Protocol + orchestrator
│   ├── platform/                   # Platform adapters
│   ├── profiles/                   # Profile descriptors
│   ├── release/                    # Release strategy + deprecation (§21.8)
│   ├── reporting/                  # Reporting
│   ├── runtimes/                   # Runtime descriptors
│   ├── security/                   # Safety/policy
│   ├── storage/                    # SQLite + future stores
│   └── tests/                      # Epic 7.x–9.x + Phase 2–4 targeted tests
├── aee-runtime/                    # AEE-4 lightweight agent runtime (Node.js)
├── bootstrap/                      # Bootstrap v1 library + manifests
├── cli/                            # CLI helpers (hermes-task)
├── data/                           # Runtime DB (dispatcher.db, WAL, SHM) — DO NOT pack
├── dispatcher/                     # Dispatcher (db, manager, models)
├── docs/                           # Documentation (AEE phase reports, guides)
├── gpt/                            # GPT Action setup guide + examples
├── logs/                           # Logs (supervisord, shadow_run) — DO NOT pack
├── prompts/                        # Prompt templates
├── reports/                        # Per-task reports + work-order reports
├── scripts/                        # compile-deps.sh, verify-deps.sh
├── supervisor/                     # supervisord unit (Abacus)
├── systemd/                        # reference systemd unit (non-Abacus)
├── tests/                          # Legacy tests + acceptance + e2e
├── .github/workflows/              # CI/CD (ci-matrix.yml)
└── README.md
```

## Roadmap (what's not yet shipped)

The items below are **not** in the shipped v1 surface. Items marked
"untracked" exist as working-tree residue (not committed) and must not be
relied upon until committed.

- **SSE event tailing for multi-client fan-out** — planned for a future
  release.
- **MCP server** — planned after Custom GPT Action runs cleanly.
- **Multi-tenant auth** — not yet implemented.
- **Observability / audit event emitter** (`aee/observability/`,
  `aee/observability_runtime/`) — **untracked working-tree residue**; not
  committed and not part of any released version. Do not rely on it.

## DO NOT pack runtime data

As of **AEE-0** (2026-07-10), the following are runtime-only and must
**never** be included in handoff tarballs, repo commits, or any artifact
shipped off-box:

- `.env`, `.api_keys.vault.json`, `CREDENTIALS.txt`, any `*.pem`/`*.key`
- `data/dispatcher.db` and its WAL/SHM sidecars (in-progress task state)
- `logs/` (per-task logs, notifier log, supervisord stdout/stderr)
- `reports/TASK-*/` (per-task rendered reports)
- `runtime_data/` (introduced in AEE-0 for any future local DBs)
- `.venv/`, `__pycache__/`, `*.pyc`, `*.egg-info/`, `dist/`, `build/`
- Anything matching `*.bak.*` or `data.bak.*/`

The repo ships a `.tarignore` (used by `tar --exclude-from`) and a
`.gitignore`. Both are kept in sync. Verify any handoff tarball before
shipping:

```bash
tar -tzf handoff.tar.gz | grep -E '(\.env|api_keys|CREDENTIALS|\.db|\.venv|__pycache__|TASK-)' \
  && echo "BAD: secrets/runtime data in tarball" \
  || echo "OK: tarball clean"
```

## Authoritative references

| Section | Reference |
|---|---|
| §21.1 Product Profile Matrix | `aee/profiles/descriptor.py::KNOWN_PROFILES` |
| §21.2 CLI UX | `aee/cli.py` (`--profile` flag) |
| §21.3 Installer | `install.sh --profile {full,mini,edge,developer}` / `install.ps1` |
| §21.4 Runtime Profile Selection | `app.py::POST /runs` → `Task.profile` |
| §21.5 Docker Profiles | `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh` |
| §21.6 Provider-Neutral Deployment | `aee/deploy/adapters/`, `host.capabilities.yaml` |
| §21.7 CI/CD Matrix | `.github/workflows/ci-matrix.yml`, `aee/ci/matrix.py` |
| §21.8 Release Strategy | `aee/release/__init__.py`, `aee/release/deprecation.py` |
| §21.9 Documentation Migration | this README (single entry point) |
| §21.10 Deprecation Plan | AEE-MINI `README.md` deprecation notice |

**Master Plan** (canonical architecture reference for all of Epic 9):
`/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md` — ADR-009 and the §21.x
chapter live here. This README is the operator-facing single entry point
that the Master Plan's §21.9 proposal calls for.

Additional in-repo documentation:

| Path | Category |
|---|---|
| [`docs/HERMES_ADAPTER_CONTRACT_MATRIX.md`](docs/HERMES_ADAPTER_CONTRACT_MATRIX.md) | Adapter contract |
| [`docs/MIGRATION_FROM_AEE_MINI.md`](docs/MIGRATION_FROM_AEE_MINI.md) | Migration guide (AEE-MINI → unified) |
| [`docs/AEE_RUNTIME_INTEGRATION_GUIDE.md`](docs/AEE_RUNTIME_INTEGRATION_GUIDE.md) | Integration guide (AEE-4 Part B freeze) |
| [`docs/runtime/Worker_Runtime_Contract.md`](docs/runtime/Worker_Runtime_Contract.md) | Runtime contract (AEE-4 Part A freeze) |
| [`docs/aee/AEE5_API_REFERENCE.md`](docs/aee/AEE5_API_REFERENCE.md) | API reference (AEE-5) |
| [`docs/aee/AEE5_CONFIGURATION.md`](docs/aee/AEE5_CONFIGURATION.md) | Configuration (AEE-5) |
| [`docs/aee/AEE5_MIGRATION_GUIDE.md`](docs/aee/AEE5_MIGRATION_GUIDE.md) | Migration guide (AEE-5) |
| [`docs/aee/AEE5_RUNTIME_REGISTRY_ARCHITECTURE.md`](docs/aee/AEE5_RUNTIME_REGISTRY_ARCHITECTURE.md) | Runtime registry architecture (AEE-5) |
| [`gpt/GPT_SETUP_GUIDE.md`](gpt/GPT_SETUP_GUIDE.md) | ChatGPT Custom GPT Action setup |

## Migration from AEE-MINI

If you are an existing AEE-MINI operator (v1.0.1), the migration path is a
**fresh install** of the unified product with `--profile mini`, not an
in-place upgrade (§21.R R4 mitigation). The AEE-MINI repo is frozen at
`1.0.1`; the AEE-MINI installer continues to work during the §21.10
deprecation window for existing B2 deployments that are not ready to
migrate. See [`docs/MIGRATION_FROM_AEE_MINI.md`](docs/MIGRATION_FROM_AEE_MINI.md)
for the full migration guide and the deprecation notice in AEE-MINI's
`README.md` for the timeline.