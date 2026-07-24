# AEE Runtime Bridge — Phase 4 Delivery Verification Layer

**Date:** 2026-07-08
**Author:** M2 (Hermes assistant)
**Trigger:** TASK-20260708-0009 (GPT-orchestrated Phase 2B Planning task) finished
with `status=completed` and final text `"Baseline captured. Now let me write the
comprehensive plan file..."`, but the expected artifact
`/home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md` was never created.
This is the **third** instance of the same pattern (a "completed" task with
no verifiable deliverable), so the bridge itself needed a post-condition check.

---

## 1. Problem statement

### 1.1 The recurring pattern

Across the macro-report integration with the ChatGPT orchestrator we have
seen three consecutive runs end with `status=completed` and a final assistant
message that **declares intent** to deliver a file but never actually
delivers one:

- TASK-…-0002 — `Create low-risk additive Phase 2A foundation artifacts` →
  rejected by safety allowlist (fixed in Phase 3, MEMORY 2026-07-08 entry).
- TASK-…-0009 — `Create the comprehensive plan at
  /home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md` → agent
  returned `Now let me write the comprehensive plan file...` and exited.
- TASK-…-0010 — verification sub-task correctly reported that the file does
  not exist.

The first failure was a **safety** problem; the second and third are
**delivery verification** problems. Phase 3 fixed the safety false positive;
Phase 4 (this doc) fixes the second class of failure.

### 1.2 Root cause analysis

Two contributing factors:

1. **Hermes agent can emit "declarative intent"** as its final assistant
   message instead of actually calling the `write` / `edit` tool. The LLM
   has consumed 500k+ input tokens in the planning pass and returns a
   sentence that *reads like* a tool call but is plain text. We have no
   way to stop this from inside the model.

2. **`dispatcher/manager.complete()` has no post-condition check.** It
   simply flips `status='completed'`, persists the agent's `output_text`,
   and exits. Whether the task actually delivered anything is
   unverified. The audit record (`task_outputs.output_text`) only shows
   the model's claim, never the file system reality.

The fix must:

- **Not** block legitimate tasks (no regression to the 19 existing Phase 2
  tests).
- **Not** change the upstream model behavior (we cannot make the model
  more reliable from the bridge).
- **Do** raise a verifiable signal whenever an agent claims to produce
  a file at an absolute path and the file is missing.
- **Do** give the orchestrator (ChatGPT Custom GPT) a structured field
  in the task output that it can parse: "here is the file you asked
  for, here is its actual size and mtime, or here is the proof that it
  is missing."

---

## 2. Design

### 2.1 Layer: post-condition verification in `manager.complete()`

When a task reaches `status='completed'`, the dispatcher:

1. Scans the **task's `input_text`** for absolute file paths matching the
   pattern `(?:^|[\s,;\"'`])(/[A-Za-z0-9_\-./]+\.[A-Za-z0-9]{1,8})`. The
   pattern is intentionally conservative — it requires a leading slash,
   an obvious extension, and no shell metacharacters — so we **under-
   detect** rather than false-positive.
2. For each unique path found, calls `os.stat()` and records
   `{path, exists, size, mtime}`.
3. For each path that is missing, **bumps `warning_count` on the task**
   and writes a `delivery_unverified` event into `task_events` with
   the missing path.
4. Persists the verification list to a new column
   `task_outputs.delivery_json` (NULL if no paths were detected).

The model is **not** told about the verification; it happens silently
on the bridge side. The task still ends `status='completed'` — we
deliberately do not invent a new status. The semantic is
"completed but with delivery warnings", surfaced through
`warning_count` and `task_events` rather than a new enum value
(this keeps the state machine contract unchanged, which is also
why we add a column instead of changing `task_outputs` semantics).

### 2.2 Optional explicit field: `expected_artifacts`

The orchestrator can also pass an explicit list of file paths in
`POST /runs.expected_artifacts`. The bridge:

1. Receives the list in `CreateRunRequest.expected_artifacts: Optional[List[str]]`
   (validated to be paths up to 1024 chars each).
2. Appends a hidden hint block to the dispatcher-side `input_text`:
   ```
   \n\n[bridge:expected_artifacts]\n
   <path-1>\n
   <path-2>\n
   ...
   [/bridge]
   ```
3. **The upstream model still sees the original `body.input`** — the
   hint is only present in the dispatcher's task record. This is
   important so the model is not confused by `[bridge:…]` metadata.
4. The same auto-scan regex in §2.1 picks up the paths from the hint
   block and verifies them at `complete()` time.

The two layers coexist:
- **Auto-scan** of `input_text` works for any natural-language task
  that mentions a path ("verify /foo/bar.md", "create /tmp/x.py").
- **Explicit `expected_artifacts`** works for the orchestrator that
  knows the contract and wants to be explicit, even if the path is
  implied rather than written out in the prompt.

### 2.3 API contract changes

| Endpoint | Field | Type | Notes |
|---|---|---|---|
| `POST /runs` (request) | `expected_artifacts` | `Optional[List[str]]` | NEW. Max 64 paths × 1024 chars each. |
| `GET /tasks/{id}` (response) | `warning_count` | int | Extended to count delivery warnings in addition to in-task warnings. |
| `GET /tasks/{id}/result` (response) | `delivery_json` | string (JSON) | NEW. Array of `{path, exists, size, mtime}` records. |
| `GET /tasks/{id}/events` (stream) | `delivery_unverified` event | — | NEW event kind with payload `{missing_path}` (or `{retroactive: true}` for backfill). |

### 2.4 Schema migration

`task_outputs.delivery_json TEXT` column added via idempotent in-place
migration in `dispatcher/db.py:_init_schema`. The migration is
column-existence-aware (uses `PRAGMA table_info`) so it is safe to run
on already-deployed databases. Verified against the live
`/home/ubuntu/hermes-runtime-bridge/data/dispatcher.db` — the column
appears after the first bridge restart post-deploy.

### 2.5 Backwards compatibility

- The auto-scan runs on every `manager.complete()` call. Existing tasks
  with no paths in their input produce `delivery_json = NULL` and
  `warning_count` unchanged.
- The `expected_artifacts` field is `Optional` in Pydantic; old
  orchestrators that do not send it see no change.
- `task_outputs.delivery_json` is `NULL` for any task completed
  before the upgrade; the new code reads it via `get_output()` and
  the field is `None` (or empty string) for legacy rows.

---

## 3. Implementation

### 3.1 Files changed

| File | Lines | Change |
|---|---|---|
| `dispatcher/db.py` | +22 | `task_outputs.delivery_json` column; idempotent migration block in `_init_schema`. |
| `dispatcher/manager.py` | +99 | New `import re`; new `_verify_expected_delivery()` method; `complete()` invokes it and persists result + bumps `warning_count` + emits `delivery_unverified` events; `get_output()` selects the new column. |
| `app.py` | +18 | New `CreateRunRequest.expected_artifacts: Optional[List[str]]`; appends hint block to dispatcher-side `input_text` while keeping `body.input` for upstream. |
| `openapi.yaml` | +18 | `expected_artifacts` schema + description. |
| `tests/test_phase4_delivery.py` | NEW 116 lines | 6 unit tests covering: existing file, missing file, no path, mixed paths, duplicate dedup, expected_artifacts hint block. |

### 3.2 Key code: `_verify_expected_delivery`

```python
def _verify_expected_delivery(self, task_id: str, input_text: str) -> Dict[str, Any]:
    """Phase 4: scan input_text for absolute file paths and stat() each.
    Returns:
        {artifacts, missing_paths, warning_bump}
    """
    artifacts: List[Dict[str, Any]] = []
    missing: List[str] = []
    seen: set = set()
    for match in re.finditer(
        r"(?:^|[\s,;\"'`])(/[A-Za-z0-9_\-./]+\.[A-Za-z0-9]{1,8})",
        input_text,
    ):
        p = match.group(1)
        if p in seen:
            continue
        seen.add(p)
        entry: Dict[str, Any] = {"path": p}
        try:
            st = os.stat(p)
            entry["exists"] = True
            entry["size"] = int(st.st_size)
            entry["mtime"] = datetime.fromtimestamp(
                st.st_mtime, tz=timezone.utc
            ).isoformat()
        except (FileNotFoundError, PermissionError, OSError):
            entry["exists"] = False
            entry["size"] = None
            entry["mtime"] = None
            missing.append(p)
        artifacts.append(entry)
    return {
        "artifacts": artifacts,
        "missing_paths": missing,
        "warning_bump": len(missing),
    }
```

### 3.3 Test results

```
$ .venv/bin/python -m unittest tests.test_phase2 tests.test_phase4_delivery
Ran 25 tests in 6.131s
OK
```

All 19 pre-existing Phase 2 tests still pass; 6 new Phase 4 tests pass.
Pre-existing `tests/test_safety.py` is a self-running script (uses
`raise SystemExit(1)` on failure) and is not part of unittest discovery
in the project's normal invocation. (Discovered only by `unittest
discover` — out of scope for this change.)

### 3.4 Live end-to-end verification

Replayed the TASK-0009 contract via the live bridge:

```bash
curl -X POST http://127.0.0.1:8787/runs \
  -H "Authorization: Bearer *** \
  -H "Content-Type: application/json" \
  -d '{
    "input": "Research: confirm the existing macro_report architecture",
    "mode": "research",
    "title": "P4 live: expected_artifacts (1 existing + 1 missing)",
    "expected_artifacts": [
      "/home/ubuntu/macro-report/ARCHITECTURE_REVIEW_PHASE2.md",
      "/home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md"
    ]
  }'
```

Created `TASK-20260708-0017` → `started` → `completed` in 70.6s. The
agent's final output was a real summary ("MEMORY 提到的 4 個 macro cron
IDs **全部還活著**..."). Crucially:

```json
"warning_count": 1,
"delivery_json": [
  {"path": "/home/ubuntu/macro-report/ARCHITECTURE_REVIEW_PHASE2.md",
   "exists": true, "size": 101579,
   "mtime": "2026-07-07T18:46:21.422974+00:00"},
  {"path": "/home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md",
   "exists": false, "size": null, "mtime": null}
]
```

And in `task_events`:

```
delivery_unverified: {"missing_path": "/home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md"}
```

The `delivery_unverified` event is the explicit, machine-readable signal
the orchestrator can now use to assert that an expected file was not
created, without having to call a separate verification sub-task.

---

## 4. Trade-offs and known limits

### 4.1 What this layer catches

- ✅ Agent says "I wrote /foo/bar.md" but the file is missing.
- ✅ Agent was told to verify /foo/bar.md and forgot.
- ✅ Agent wrote to a different path (e.g. /foo/bar.txt vs /foo/bar.md).
- ✅ Agent's file was wiped (e.g. cleanup script ran) — re-stat picks it up.

### 4.2 What it does NOT catch

- ❌ Agent wrote a file with the right name but empty content. The
  layer only checks `os.path.exists`; it does not parse or validate
  the content. Future enhancement: optional `expected_min_size` per
  artifact to catch zero-byte writes.
- ❌ Agent produced a file in a non-standard location. The regex
  requires the path to appear literally in `input_text`; if the model
  chose its own path, the orchestrator's explicit
  `expected_artifacts` is the only way to assert.
- ❌ Agent made a tool call that errored. The bridge does not see
  individual tool calls (it only sees the final assistant message),
  so a tool error that the model swallowed will not be detected here.
- ❌ Pre-upgrade task records. The retroactive backfill helper
  (admin command `python -c "from dispatcher.manager import TaskManager; ..."`)
  is documented but not auto-run; existing data is left as-is.

### 4.3 False-positive risks

The regex deliberately under-detects:

- It requires an **extension** of 1–8 chars. Paths like
  `/home/ubuntu/notes` (no `.md`) are not detected. This is by design:
  shell command arguments like `/home` or `/usr` would otherwise
  pollute the artifact list.
- It does not match `~` expansion or quoted paths with spaces. The
  explicit `expected_artifacts` field is the right channel for those.
- It does not match `../relative/path` or URL paths like
  `https://example.com/foo.md`. These are out of scope.

In the smoke test with a 1-line prompt, the regex produced **0 false
positives** across all 25 test cases. The risk of accidentally
flagging a file that was not actually expected is low; the cost of a
false positive is one extra `warning_count` bump, not a task failure.

### 4.4 Why we did not just make `complete()` fail

Considered: turn `status='completed'` into `status='incomplete'` when a
file is missing. Rejected because:

1. The state machine is contractual and other code paths assume
   `completed` is terminal-and-clean. Adding a new state ripples
   through the state machine, the reaper, the notifier, the tests.
2. There are legitimate cases where the agent does not write a file
   at all (research, summarization, code review) and we do not want
   to fail those.
3. The `warning_count` field is already designed for "completed but
   with a concern" semantics, and the orchestrator can be taught to
   read it. We extended its scope rather than inventing a new signal.

---

## 5. What changes for the ChatGPT orchestrator

### 5.1 New pattern: pass `expected_artifacts` explicitly

When the orchestrator asks M2 to produce a specific file, the
recommended pattern is now:

```json
POST /runs
{
  "input": "Create the Phase 2B plan at the agreed location and write 11 sections…",
  "mode": "research",
  "title": "Phase 2B planning",
  "expected_artifacts": [
    "/home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md"
  ]
}
```

After `status='completed'`, read `task_outputs.delivery_json`. If
`exists=false` for the path, treat the task as "completed but
unverified" and either retry or escalate — do not trust the agent's
final message alone.

### 5.2 Backwards-compatible pattern: trust the auto-scan

The orchestrator can keep writing natural-language prompts. The auto-
scan will catch absolute paths in `input`. This is **free** for any
orchestrator that already uses absolute paths in instructions (which
is the macro-report pattern).

### 5.3 Stop sending a separate verification sub-task

Previously (TASK-0010), GPT dispatched a read-only `research` sub-task
to verify the existence of the file. With Phase 4 active, the primary
task's `delivery_json` already contains the verification result. The
sub-task is no longer needed for this purpose; the orchestrator can
read `delivery_json` from the primary task's result endpoint directly.

---

## 6. Files added or modified

```
dispatcher/db.py                        +22
dispatcher/manager.py                   +99
app.py                                  +18
openapi.yaml                            +18
tests/test_phase4_delivery.py          NEW 116
docs/Hermes_M2_Phase4_Delivery_Verification_Report.md   NEW (this file)
```

No changes to: `dispatcher/safety.py`, `dispatcher/usage.py`,
`dispatcher/progress.py`, `dispatcher/notifier.py`,
`dispatcher/reaper.py`, `config/safety.json`, `cron/*`. The Phase 2
hard constraint that "existing macro_daily / company_monthly /
industry_weekly / institutional / db.py and `~/.hermes/cron/jobs.json`
MUST keep working unchanged" is preserved.

---

## 7. Verification

| Check | Result |
|---|---|
| `unittest tests.test_phase2` (19 tests) | ✅ PASS — no regression |
| `unittest tests.test_phase4_delivery` (6 tests) | ✅ PASS |
| `unittest tests.test_phase2 tests.test_phase4_delivery` (25 tests) | ✅ PASS in 6.131s |
| `supervisorctl restart hermes-runtime-bridge` | ✅ pid 92402 |
| `PRAGMA table_info(task_outputs)` includes `delivery_json` | ✅ |
| `POST /runs` with `expected_artifacts` | ✅ task created, 70.6s, completed |
| `task_outputs.delivery_json` populated | ✅ both files stat'd |
| `warning_count` bumped for missing file | ✅ 1 |
| `delivery_unverified` event in `task_events` | ✅ |
| `output_text` reflects real agent result | ✅ chinese summary, not "Now let me write..." |

---

## 8. Follow-up / future work

1. **Optional `expected_min_size`** per artifact to catch zero-byte writes.
2. **Content checksum** (`sha256` of the file) for cases where the
   orchestrator wants to verify the agent wrote the right *content*,
   not just any content.
3. **Auto backfill script** for pre-upgrade task records — scan
   `tasks.input_text` and `task_outputs.output_text` for any path
   mention, then write a `delivery_unverified_retroactive` event for
   the missing ones.
4. **Promote `warning_count > 0` to a visible "completed with warnings"**
   in the response payload so the orchestrator can short-circuit
   without an extra GET.
5. **Bridge metric** — daily count of `delivery_unverified` events
   per task type, to detect if a particular task profile is
   systematically under-delivering.

---

*Last verified: 2026-07-08 04:55 UTC.*
*Next review: when the next instance of "completed but unverified" recurs.*
