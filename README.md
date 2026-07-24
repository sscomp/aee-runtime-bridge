# AEE Runtime Bridge

A thin, safe OpenAI-Custom-GPT-Action / MCP-friendly facade in front of the
Hermes M2 (Abacus.ai) `/v1/runs` API. As of Epic 9 (ADR-009), this repository
is the **unified product** — the single entry point for the AEE Runtime
across all four product profiles (`full`, `mini`, `edge`, `developer`).

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
- **Installer**: `install.sh --profile {full,mini,edge,developer}`.
  Single installer absorbs AEE-MINI's hardening (idempotent pre-flight, `aee`
  system user, `0600` env file, smoke test). Profile switch on an existing
  install is **rejected** ("profile change requires uninstall + reinstall").
- **Docker**: `docker run aee:2.0.0 --profile {full,mini,edge,developer}`.
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
Custom GPT Action schema box).

## Local run (foreground)

```bash
cd ~/hermes-runtime-bridge
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env
# edit .env — fill in HERMES_API_KEY (and a new BRIDGE_API_KEY)
./.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8787
```

## Production run (Abacus.AI host — supervisord)

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

## Cloudflare Tunnel

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

## Safety guard

`POST /runs` rejects input matching any pattern in `app.py::DANGEROUS_PATTERNS`.
Run `PYTHONPATH=. ./.venv/bin/python tests/test_safety.py` to self-test.
The Python self-test covers 19 dangerous patterns and 9 clean phrases.

The curl version is in `tests/test_unsafe.sh`.

> This is a *first line of defence*, not a complete security model. Hermes'
> own toolset config and approval gates are the real enforcement layer.

## AEE targeted tests

The `aee/tests/` directory contains per-Epic-slice targeted test files
(`test_aee81_*.py` … `test_aee99_*.py`). Run a single slice:

```bash
cd ~/hermes-runtime-bridge
PYTHONPATH=. ./.venv/bin/python -m unittest aee.tests.test_aee99_documentation_migration -v
```

Run the full Epic 9 regression sweep:

```bash
PYTHONPATH=. ./.venv/bin/python -m unittest discover -s aee/tests -p "test_aee9*.py" -v
```

## Test scripts

| Script | What it does |
|---|---|
| `tests/test_health.sh`            | Hit `/health` and confirm Hermes reachable. |
| `tests/test_create_run.sh`        | Start a run, save `run_id` to `/tmp/bridge_last_run_id`. |
| `tests/test_get_run.sh`           | Poll `/runs/{id}` and `/runs/{id}/summary`. |
| `tests/test_stop_run.sh`          | Request cancellation. |
| `tests/test_unsafe.sh`            | Send 16 dangerous payloads; expect 400 each. |
| `tests/test_safety.py`            | Pure-Python blocklist unit test (19 dangerous, 9 clean). |

All scripts honour `BRIDGE_API_KEY` and an optional `BASE` arg.

## Layout

```
~/hermes-runtime-bridge/
├── app.py                          # FastAPI app
├── openapi.yaml                    # Custom GPT Action schema
├── requirements.txt
├── .env.example
├── .env                            # not committed, chmod 600
├── .venv/                          # uv-managed venv
├── aee/                            # unified AEE package
│   ├── __init__.py                 # __version__ == "2.0.0-rc1"
│   ├── cli.py                      # --profile, --version
│   ├── profiles/descriptor.py      # KNOWN_PROFILES, DEFAULT_PROFILE (SoT)
│   ├── adapters/hermes_adapter.py  # unified Hermes adapter
│   ├── release/                    # §21.8 Release Strategy
│   └── tests/test_aee9*.py         # Epic 9.x targeted tests
├── supervisor/
│   └── hermes-runtime-bridge.conf  # supervisord unit (used on Abacus)
├── systemd/
│   └── hermes-runtime-bridge.service  # reference systemd unit
├── docs/
│   ├── HERMES_ADAPTER_CONTRACT_MATRIX.md  # moved from AEE-MINI per §21.9
│   └── (existing AEE/Hermes_M2 phase reports — archived)
├── tests/
│   ├── test_safety.py              # Python blocklist unit test
│   ├── test_health.sh
│   ├── test_create_run.sh
│   ├── test_get_run.sh
│   ├── test_stop_run.sh
│   └── test_unsafe.sh
├── logs/                           # supervisord stdout/stderr
└── README.md
```

## What's intentionally not in v1

* Per-run audit SQLite (planned for v1.2 in the SOP).
* SSE event tailing for multi-client fan-out (planned for v3).
* MCP server (planned for v2, after Action runs cleanly).
* Multi-tenant auth.

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
| §21.3 Installer | `install.sh --profile {full,mini,edge,developer}` |
| §21.4 Runtime Profile Selection | `app.py::POST /runs` → `Task.profile` |
| §21.5 Docker Profiles | `Dockerfile` at repo root |
| §21.6 Provider-Neutral Deployment | `aee/deploy/adapters/` |
| §21.7 CI/CD Matrix | `aee/ci/` + workflow spec |
| §21.8 Release Strategy | `aee/release/__init__.py` |
| §21.9 Documentation Migration | this README (single entry point) |
| §21.10 Deprecation Plan | AEE-MINI `README.md` deprecation notice |

**Master Plan** (canonical architecture reference for all of Epic 9):
`/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md` — ADR-009 and the §21.x
chapter live here. This README is the operator-facing single entry point
that the Master Plan's §21.9 proposal calls for.

## Migration from AEE-MINI

If you are an existing AEE-MINI operator (v1.0.1), the migration path is a
**fresh install** of the unified product with `--profile mini`, not an
in-place upgrade (§21.R R4 mitigation). The AEE-MINI repo is frozen at
`1.0.1`; the AEE-MINI installer continues to work during the §21.10
deprecation window for existing B2 deployments that are not ready to
migrate. See the deprecation notice in AEE-MINI's `README.md` and the
full timeline in Master Plan §21.10.