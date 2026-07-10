# Hermes Runtime Bridge

A thin, safe OpenAI-Custom-GPT-Action / MCP-friendly facade in front of the
Hermes M2 (Abacus.ai) `/v1/runs` API.

```
ChatGPT Custom GPT Action          (or MCP client)
        │  Authorization: Bearer BRIDGE_API_KEY
        ▼
Hermes Runtime Bridge   (this service, 127.0.0.1:8787)
        │  Authorization: Bearer HERMES_API_KEY   (different key)
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
├── supervisor/
│   └── hermes-runtime-bridge.conf  # supervisord unit (used on Abacus)
├── systemd/
│   └── hermes-runtime-bridge.service  # reference systemd unit
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
tar -tzf handoff.tar.gz | grep -E '(^\./\.env|api_keys|CREDENTIALS|\.db|\.venv|__pycache__|TASK-)' \
  && echo "BAD: secrets/runtime data in tarball" \
  || echo "OK: tarball clean"
```
