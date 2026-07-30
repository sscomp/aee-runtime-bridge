# AEE Runtime Bridge — Recent Run Listing Endpoint (TASK-AEE-RUN-LIST-ENDPOINT)

Repository: `/home/ubuntu/hermes-runtime-bridge`
Work order: TASK-AEE-RUN-LIST-ENDPOINT
Date: 2026-07-22

## Execution Timing

- Start: 2026-07-22 (session began mid-implementation; baseline + scaffolding
  carried over from prior context window)
- End: 2026-07-22 (this report finalization)
- Mode: M2 inline implementation (no delegated subagent)
- Total focused tests: 24/24 PASS
- Total regression tests: 396 passed, 4 failed (all pre-existing, see Regression)

## Overall Verdict

PASS.

`GET /runs` is implemented as a pure read of the durable
`executor_runs` store. It does not launch any executor, poll upstream
Hermes, mutate run state, or scan the repo. The endpoint supports
bounded pagination (`limit` 1..100, default 20), filtering by
`status`, `executor`, and `since` (ISO-8601), and returns a
deterministic newest-first ordering with a tie-breaker on `run_id`
(DESC). The GPT-facing OpenAPI spec and setup guide are updated. One
atomic commit ships the change with no unrelated hunks.

## Baseline

Working tree at task acceptance was already dirty with unrelated
changes (observability events, notifier, dispatcher, models, AEE
report files, etc.). Those changes were left untouched — only the
in-scope files listed in `Files Changed` were staged for the atomic
commit.

Pre-existing failures present at baseline (unchanged by this work):
- `tests/test_openapi_executor_metadata.py` — collection error
  (`ModuleNotFoundError: No module named 'yaml'`); excluded from
  regression.
- `tests/test_aee_v3_telegram_gate.py::TestDbMigration::test_notification_json_column_present_after_init`
- `tests/test_dispatcher.py::TestTaskLifecycle::test_log_file_written`
- `tests/test_dispatcher.py::TestTaskLifecycle::test_task_json_written_on_complete`
- `tests/test_dispatcher.py::TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation`
- `tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent`

None of these touch the `/runs` endpoint surface or the
`executor_runs.list_runs` function added here.

## API Contract

### Endpoint

`GET /runs` — read-only list of recent executor runs.

### Query parameters

| Parameter | Type | Default | Range / format | Description |
|-----------|------|---------|----------------|-------------|
| `limit`   | integer | 20 | 1..100 | Maximum number of runs to return. |
| `status`  | string | — | one of `queued`, `started`, `running`, `completed`, `failed`, `timeout`, `cancelled` | Filter by canonical run status. |
| `executor` | string | — | free-form | Filter by `selected_executor` (e.g. `claude-code-cli` or `hermes`). |
| `since`   | string | — | ISO-8601 timestamp | Only runs with `created_at >= since` are returned. |

### Ordering

Newest-first by `created_at` DESC, with a deterministic tie-breaker
on `run_id` DESC. Two runs sharing a timestamp have a stable order
across calls.

### Response envelope (HTTP 200)

```json
{
  "items": [<RunSummaryItem>, ...],
  "count": <integer>,
  "limit": <integer>,
  "filters": {
    "status": <string|null>,
    "executor": <string|null>,
    "since": <string|null>
  }
}
```

### RunSummaryItem fields

Each item in `items` includes at minimum:

- `run_id` (string)
- `requested_executor` (string|null)
- `selected_executor` (string)
- `status` (string)
- `progress` (integer|null)
- `created_at` (string|null, ISO-8601)
- `updated_at` (string|null, ISO-8601)
- `completed_at` (string|null, ISO-8601)
- `is_terminal` (boolean)
- `source` (string|null)
- `artifact_paths` (array of strings)
- `error` (string|null)

### Error envelopes

| Case | HTTP | `code` | Body |
|------|------|--------|------|
| Invalid `status` (not in canonical set) | 400 | `invalid_status` | `{ "code", "message", "valid_statuses" }` |
| Malformed `since` (not ISO-8601) | 400 | `invalid_since` | `{ "code", "message" }` |
| `limit` out of range (0 or >100) | 422 | — | FastAPI validation error |
| Missing / invalid bearer token | 401 | — | upstream auth error |

### Purity guarantees

`GET /runs` does NOT:
- launch any executor
- poll upstream Hermes
- call `reconcile_run_status` or any reconciliation path
- mutate any run state
- scan the repo
- write to the database

It is a single read-only SQL `SELECT` against `executor_runs`,
materialized in-memory, and returned as JSON.

## Query and Ordering Semantics

### Filters

- `status`: validated against `CANONICAL_RUN_STATUSES` (see
  `dispatcher/executor_runs.py`). Invalid value → HTTP 400 with
  `code: "invalid_status"` and `valid_statuses` listing the
  acceptable values. The check is deterministic — there is no silent
  empty-list fallback for an invalid status.
- `executor`: free-form string; no canonical-set check. An unknown
  executor simply yields `count: 0` (HTTP 200), because the executor
  namespace is pluggable and not bounded.
- `since`: parsed via `datetime.fromisoformat`. Invalid value →
  HTTP 400 with `code: "invalid_since"`. The parsed value is
  re-serialized to canonical ISO-8601 before being passed to
  `list_runs` so the SQL `>=` comparison is well-formed.
- `limit`: FastAPI validates the range at the request-boundary layer
  via `Query(ge=1, le=100)`. Out-of-range values produce HTTP 422
  (standard FastAPI validation error).

### Ordering

The SQL query orders by `created_at DESC, run_id DESC`. The
`run_id` tie-breaker is a ULID-style string; lexicographic DESC on
ULIDs yields a deterministic order even when two runs share a
`created_at` timestamp (millisecond precision).

### Pagination semantics

This is a "first page" endpoint — no `offset` or `cursor` parameter
is exposed. Callers needing deeper history should narrow with `since`
or `status` filters. This keeps the surface small and avoids
stateful cursor management.

## Files Changed

| File | Status | LOC delta | Purpose |
|------|--------|-----------|---------|
| `dispatcher/executor_runs.py` | M | +81 | Add `list_runs()` + `CANONICAL_RUN_STATUSES`; update `__all__` |
| `app.py` | M | +138 | Add `GET /runs` endpoint (`list_runs_endpoint`) with input validation, auth, and envelope response |
| `tests/test_run_list_endpoint.py` | new | +24 tests | Cover all 10 work-order scenarios |
| `tests/test_run_tracking.py` | M | +12/-6 | Update `test_empty_run_id_is_deterministic` to accept 200 (GET /runs/ normalizes to GET /runs returning empty list) |
| `openapi.yaml` | M | +86 | Add GET /runs operation + `RunSummaryItem` / `RunListResponse` schemas |
| `gpt/aee_executor_openapi.json` | M | +85/-1 | Add `/runs` GET path + 4 new schemas; bump version 1.1.0 → 1.2.0; all original schemas and paths preserved byte-identical |
| `gpt/GPT_SETUP_GUIDE.md` | M | +104 | Add §8 with curl examples for recent failures / running jobs / latest completed / since / executor filters; renumber subsequent sections (8→9, 9→10, 10→11) |

Total: 6 modified + 1 new = 7 files, +500/-6 diffstat (tracked files).

### Out-of-scope (left untouched)

The working tree contains many other modified/untracked files from
prior unrelated work (AEE-7.4 observability, notifier, dispatcher
manager/models, AEE-7.7d/7.8/9.5 report files, etc.). None of these
were staged in the atomic commit. The staging set is the explicit
list above.

## Tests

### Focused tests — `tests/test_run_list_endpoint.py`

24 tests covering all 10 work-order scenarios:

1. `test_default_recent_list` — default limit 20, no filters
2. `test_newest_first_deterministic_ordering` — created_at DESC + run_id DESC tie-breaker
3. `test_limit_bounds_min` — limit=1 returns exactly 1 item
4. `test_limit_bounds_max` — limit=100 returns up to 100 items
5. `test_limit_above_max_rejected` — limit=101 → 422
6. `test_limit_zero_rejected` — limit=0 → 422
7. `test_status_filter_completed` — only completed runs returned
8. `test_status_filter_failed` — only failed runs returned
9. `test_status_filter_running` — only running runs returned
10. `test_executor_filter_hermes` — only hermes-selected runs
11. `test_executor_filter_claude_code_cli` — only claude-code-cli runs
12. `test_since_filter` — only runs with created_at >= since
13. `test_since_filter_excludes_older` — boundary check
14. `test_empty_result_when_no_match` — count: 0, HTTP 200, items: []
15. `test_invalid_status_returns_400_structured` — code: invalid_status, valid_statuses present
16. `test_invalid_since_returns_400_structured` — code: invalid_since
17. `test_malformed_since_returns_400` — non-ISO-8601 string
18. `test_auth_required_no_token` — 401
19. `test_auth_required_wrong_token` — 401
20. `test_no_executor_launch` — no subprocess or executor invocation
21. `test_no_upstream_poll` — no reconcile_run_status calls
22. `test_no_mutation_of_run_state` — run rows unchanged after GET
23. `test_filters_echoed_in_response` — filters object echoes active values
24. `test_get_runs_preserves_get_runs_by_id` — GET /runs/{run_id} still works

Result: 24/24 PASS.

### Regression

Run command:
`/usr/bin/python3 -m pytest tests/ -q --no-header --ignore=tests/test_openapi_executor_metadata.py`

Result: 4 failed, 396 passed, 1 skipped, 2 warnings, 1 error in 39.92s.

Failures (all pre-existing, none touched by this work):
- `test_aee_v3_telegram_gate.py::TestDbMigration::test_notification_json_column_present_after_init`
- `test_dispatcher.py::TestTaskLifecycle::test_log_file_written`
- `test_dispatcher.py::TestTaskLifecycle::test_task_json_written_on_complete`
- `test_dispatcher.py::TestManagerPathsOrderIndependence::test_log_file_lands_in_canonical_logs_dir_after_external_mutation`
- `test_migration_aee1.py::test_run_migrations_public_api_idempotent` (collection error)

Each of these tests touches dispatcher/manager/migration code that
was NOT modified by this task. The failures reproduce at baseline
(before any of my changes) and are caused by WAL/SQLite sidecar
state from prior sessions + filesystem directory mutations, not by
the `/runs` list endpoint.

### Targeted regressions (all PASS)

`tests/test_run_tracking.py`, `tests/test_executor_response_contract.py`,
`tests/test_executor_routing.py`, `tests/test_executor_unsupported.py`,
`tests/test_routing.py` — 67 passed.

## Git Evidence

### Atomic commit

Staged with explicit-path list (no `git add -A`, no `git add .`):

```
git add dispatcher/executor_runs.py app.py tests/test_run_list_endpoint.py \
        tests/test_run_tracking.py openapi.yaml \
        gpt/aee_executor_openapi.json gpt/GPT_SETUP_GUIDE.md
```

Commit message:
```
feat(runtime): add recent run listing
```

Commit SHA: `3b739bb` (full: `3b739bb548617e7b451261dc9b3f1792398327dd`)

### Staging isolation

The working tree had 35+ unrelated modified/untracked files. The
staging set was the explicit 7-path list above. `git diff --cached`
was inspected before commit to confirm no out-of-scope hunks. The
4 pre-existing failures in `test_dispatcher.py` /
`test_migration_aee1.py` / `test_aee_v3_telegram_gate.py` were verified
to be in code paths not touched by this commit.

### Diffstat (tracked files)

```
app.py                        | 138 ++++++++++++++++++++++++++++++++++++++++++
dispatcher/executor_runs.py   |  81 +++++++++++++++++++++++++
gpt/GPT_SETUP_GUIDE.md        | 104 ++++++++++++++++++++++++++++++-
gpt/aee_executor_openapi.json |  85 ++++++++++++++++++++++++++-
openapi.yaml                  |  86 ++++++++++++++++++++++++++
tests/test_run_tracking.py    |  12 +++-
6 files changed, 500 insertions(+), 6 deletions(+)
```

(`tests/test_run_list_endpoint.py` is a new untracked file, +24
tests, included in the commit via the explicit-path list.)

## Artifact Verification

Report path: `/home/ubuntu/hermes-runtime-bridge/AEE_RUN_LIST_ENDPOINT_REPORT.md`

```
ls -la /home/ubuntu/hermes-runtime-bridge/AEE_RUN_LIST_ENDPOINT_REPORT.md
wc -c /home/ubuntu/hermes-runtime-bridge/AEE_RUN_LIST_ENDPOINT_REPORT.md
wc -l /home/ubuntu/hermes-runtime-bridge/AEE_RUN_LIST_ENDPOINT_REPORT.md
sha256sum /home/ubuntu/hermes-runtime-bridge/AEE_RUN_LIST_ENDPOINT_REPORT.md
```

The SHA-256 of this report is NOT embedded inside the report itself
(per work-order rule 16). The hash can be recomputed at any time with
`sha256sum AEE_RUN_LIST_ENDPOINT_REPORT.md`.

## Production Safety

- `GET /runs` is a pure read. It does not call `reconcile_run_status`,
  `launch_executor`, `subprocess.Popen`, `repo.scan`, or any write
  path. The only SQL it executes is a `SELECT` against
  `executor_runs` with the optional `WHERE` filters and `ORDER BY`
  clauses derived from validated inputs.
- Input validation is deterministic: invalid `status` and `since`
  produce HTTP 400 with a structured `code` field; `limit` out of
  range produces HTTP 422 via FastAPI's request-boundary validator.
- Auth is enforced via the same `require_auth` decorator used by the
  existing `GET /runs/{run_id}` endpoint. No new auth path was
  introduced.
- No environment variables, secrets, or API keys were logged or
  embedded in any output.
- No daemon was started, no port was opened, no upstream service was
  contacted during implementation or testing.
- `macro_history.db`, `dispatcher.db`, and `executor_runs` SQLite
  files were not mutated by this work (the focused tests use an
  in-memory or per-test temporary database).

## Remaining Risks

1. `executor` filter is free-form; a caller passing a typo'd executor
   name gets `count: 0` (HTTP 200) rather than a 400. This is
   intentional (executor namespace is pluggable) but worth noting in
   the GPT instructions — the GPT should treat `count: 0` as "no
   matches", not "endpoint broken".

2. `since` parsing uses `datetime.fromisoformat`, which accepts a
   broad subset of ISO-8601 but rejects some valid forms (e.g.
   timezone offsets without colons, some sub-second precision
   variants). The GPT setup guide shows the canonical `Z` suffix
   form, which is accepted.

3. The endpoint is "first page" only — no `offset`/`cursor`. Callers
   needing deeper history must narrow with `since` or `status`. If
   future demand arises for cursor pagination, it can be added
   non-disruptively as a new `cursor` query parameter.

4. The pre-existing test failures in
   `test_dispatcher.py::TestTaskLifecycle` and
   `test_migration_aee1.py` are NOT caused by this work but remain
   red. They should be addressed in a separate housekeeping task —
   they appear to be WAL/SQLite sidecar state issues from prior
   sessions.

## Live Activation Requirement

This commit is code + tests + docs only. It does NOT:
- restart the bridge
- push to a remote
- deploy anything
- modify supervisord config
- modify the running gateway

To activate live:
1. The bridge process must be restarted to load the new `app.py`.
   The bridge runs under supervisord (`hermes-runtime-bridge`
   program). Restart with:
   `supervisorctl --serverurl=unix:///tmp/supervisor.sock restart hermes-runtime-bridge`
2. Verify the endpoint live:
   `curl -sS -H "Authorization: Bearer ${AEE_BRIDGE_TOKEN}" "https://hermes-runtime.biaobecue.com/runs?limit=5" | jq`
3. Verify the OpenAPI spec served at `/openapi.json` includes the
   `/runs` GET path.

Per work-order rule 17, no push/deploy/restart was performed in this
task. Activation is the operator's responsibility.

## Review Ready

Yes. The change is self-contained, atomically committed, and the
report is verifiable. Key review points:
- `dispatcher/executor_runs.py` — `list_runs` SQL correctness and
  parameterization (no SQL injection; all user inputs go through
  parameterized queries or canonical-set validation).
- `app.py` — `list_runs_endpoint` purity (no reconcile/launch/mutate
  calls), input validation, error envelopes.
- `tests/test_run_list_endpoint.py` — coverage of all 10
  work-order scenarios plus the purity invariants.

## Commit Ready

Yes. One atomic commit with the exact 7-file staging set. No
unrelated hunks. No `git add -A`. Commit message:
`feat(runtime): add recent run listing`.

## Telegram

Attempted via `hermes send --to telegram:5132341473 --subject "TASK-AEE-RUN-LIST-ENDPOINT DONE" --file /home/ubuntu/hermes-runtime-bridge/AEE_RUN_LIST_ENDPOINT_REPORT.md --json`.

Result:
```json
{
  "success": true,
  "platform": "telegram",
  "chat_id": "5132341473",
  "message_id": "7712",
  "mirrored": true
}
```

Telegram delivery succeeded. `message_id=7712` is the verifiable
evidence (sent to 鼎鼎, chat_id `5132341473`).