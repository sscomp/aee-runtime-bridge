# Legacy HTTP bridge and product profiles

This reference covers the compatibility HTTP bridge in `app.py` and the
profile-aware CLI, installer and Docker entrypoint. It is maintained alongside
their implementation. The [MCP-first README](../README.md) is the entry point for
OpenAI Secure MCP Tunnel onboarding. Legacy profile installation does not install
or qualify that restricted MCP route; obtain an operator-approved MCP revision
and follow [operations](operations.md) for that route.

## Profile matrix

The canonical order and capabilities come from
[`aee/profiles/descriptor.py`](../aee/profiles/descriptor.py). These are descriptor
values; they do not grant additional remote MCP permissions. The default profile
is `full`; an unknown profile is rejected rather than silently replaced.

| Capability | `full` | `mini` | `edge` | `developer` |
|---|---|---|---|---|
| Dispatch | allowed | allowed | blocked | allowed |
| Cron creation | allowed | blocked | blocked | blocked |
| Subagent delegation | allowed | blocked | blocked | allowed |
| Long-running pipelines | allowed | blocked | blocked | blocked |
| Graph queries | full | subset | read_only | sandbox |
| Observability events | full | subset | read_only | sandbox |
| DB writes | full | dispatch_only | disabled | tempdir_only |
| Production DB access | full | full | read_only | blocked |
| Toolset | full | terminal_file_web_subset | file_read_web_read | full_sandbox |

## Profile selection and installer

The CLI, `install.sh` and Docker entrypoint share the canonical profile parser.
For a read-only installation plan, run from the repository root:

```bash
bash install.sh --profile mini --dry-run
python3 -m aee.cli --profile edge install --dry-run
```

`install.sh` defaults to dry-run. An explicit `--execute` delegates to the
existing BootstrapRunner (stages 02-07): it may create a venv, install locked
dependencies, run health/smoke checks and write `AGENT_READY`. It propagates the
CLI result (0 for success, 4 for pre-flight or stage failure); missing Python
or installer modules fail before dispatch (64 or 65). Provision credentials
separately and approve the installation before opting in. See the
[bootstrap onboarding contract](aee/bootstrap/onboarding.md).
A profile switch on an existing install is rejected by the
[installer backend](../aee/installer/backend.py); it requires uninstall/reinstall.
This reference does not claim that the wrapper creates system users, provisions
credentials or performs fleet deployment.

Docker uses one image with runtime profile selection:

```bash
docker run aee:2.0.0-rc1.gamma --profile mini
```

The [Dockerfile](../Dockerfile) and
[`docker-entrypoint.sh`](../docker-entrypoint.sh) define the image and selection
surface. With no command, the entrypoint prints profile information; it does not
start a gateway. `edge` sets `AEE_DB_READ_ONLY=1`; `developer` selects a temporary
DB. Operator-supplied commands and credentials remain separate.

## Endpoints

These are legacy `app.py` HTTP endpoints (default loopback port 8787), separate
from the five-tool restricted MCP gateway:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | no | Liveness and upstream reachability |
| POST | `/runs` | bearer | Start a run and return `run_id` |
| GET | `/runs/{run_id}` | bearer | Poll status and result |
| GET | `/runs/{run_id}/summary` | bearer | Read a concise summary |
| POST | `/runs/{run_id}/stop` | bearer | Request cancellation |

Source: [`app.py`](../app.py) route decorators and
[`openapi.yaml`](../openapi.yaml). See [GPT Action setup](../gpt/GPT_SETUP_GUIDE.md).
The `/health` route is not proof of successful inference. Keep diagnostic output
private and do not use legacy HTTP credentials as tunnel-forward credentials.

## Safety guard

Legacy dispatch forwards its profile to `dispatcher.safety.evaluate` through
`app.py::danger_check`. The [safety policy](../dispatcher/safety.py) applies
blocklist, allowlist, approval and profile checks; a rejected request must not
reach dispatch. Text pattern checks alone are not complete isolation. The
restricted MCP authorization boundaries remain in the
[security model](security-model.md).

## Layout and runtime data

| Path | Purpose |
|---|---|
| `app.py`, `openapi.yaml` | Legacy HTTP bridge and schema |
| `aee/profiles/descriptor.py` | Canonical profile descriptors |
| `aee/cli.py`, `aee/installer/` | CLI, pre-flight and bootstrap |
| `install.sh`, `Dockerfile`, `docker-entrypoint.sh` | Installation and Docker selection |
| `dispatcher/` | Legacy task dispatch and safety policy |
| `gpt/`, `docs/` | GPT Action and operator documentation |

**DO NOT pack** private environments, credentials, runtime databases (`data/`),
job stores, logs or historical evidence in source/image publication. See
[`.dockerignore`](../.dockerignore) and [operations](operations.md).

## Master Plan, migration and archived adapter matrix

The historical `AEE_MASTER_PLAN.md` defines the Epic 9 context (§21.1 profiles,
§21.3 installer, §21.5 Docker, §21.9 documentation migration and §21.10
deprecation). It is external historical material and is not packaged in this
repository; no host-specific path is required for current onboarding.
The repository-owned [migration guide](MIGRATION_FROM_AEE_MINI.md) records ADR-009,
the fresh-install `--profile mini` path and the deprecation timeline. Its old
host-location references are historical context, not portable prerequisites.

The current [Hermes adapter contract matrix](HERMES_ADAPTER_CONTRACT_MATRIX.md)
references `aee/adapters/hermes_adapter.py` and records the §21.9 move from
AEE-MINI. The external frozen AEE-MINI archive is a separate historical input.
Historical-preservation tests need authentic, source-backed fixture snapshots;
this current matrix is not a substitute for that archive or proof of its state.
