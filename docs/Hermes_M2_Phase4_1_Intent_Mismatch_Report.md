# Hermes Runtime Bridge — Phase 4.1 Intent-Mismatch Detection

**Date:** 2026-07-08
**Author:** M2 (Hermes assistant)
**Supersedes:** §6 of Phase 4 report (delivery verification only) — adds a higher-priority intent-mismatch event for the LLM "I said I would but didn't" failure mode.

**Trigger:** Two consecutive GPT-orchestrated task failures (TASK-20260708-0017, TASK-20260708-0018) where Phase 4 correctly raised `warning_count=1` and `delivery_unverified`, but the orchestrator still had to read the agent's prose to know it was a "stuck on intent" failure. We need a higher-signal event the orchestrator can short-circuit on without parsing free text.

---

## 1. The bug Phase 4.1 fixes

Across 4 consecutive macro-report Phase 2B retries, the M2 agent produced a final assistant message that looks like this:

| # | Final assistant message tail |
|---|---|
| TASK-0009 | "Now let me write the comprehensive plan file. The plan must be self-contained, cover all 11 sections, and include the safe extraction plan, fallback strategies, and a ready-to-dispatch M2 prompt." |
| TASK-0010 | "File does not exist... no Phase 2B proposal file has been created" |
| TASK-0017 | "Now let me write the plan file." |
| TASK-0018 | "Verified. Empty directories exist, and helpers confirmed across 3 files. Now writing the plan." |
| TASK-0025 (live verify) | "Let me write the plan. The user asked for a 'plan summary' + 'di..." |

**Pattern**: in 4/5 cases, the LLM emits a "now let me write" / "now writing" / "will create" sentence as the FINAL assistant message. Internally the LLM has already decided "the existing 99KB ARCHITECTURE_REVIEW is sufficient; I don't need a separate plan file" but the final-message generator still emits the declarative-intent sentence as if a follow-up tool call is coming. The tool call never comes. The task returns.

**Phase 4 limitation**: it raises `warning_count=1` and `delivery_unverified` for the missing file, but does not distinguish this case from "I tried to write but write_file failed" or "I wrote to a different path". The orchestrator has to read the agent's prose to know the LLM got stuck on intent.

**Phase 4.1 fix**: emit a dedicated `intent_mismatch` event with `severity: high` and `recommended_action: retry_with_explicit_write_instruction` when BOTH:
  1. `warning_count > 0` (some expected artifact is missing)
  2. the agent's final output_text contains one of the declarative-intent patterns in the tail (last 600 chars)

The orchestrator can now short-circuit on the event kind alone — no prose parsing required.

---

## 2. Design choices

### 2.1 Two-condition guard (not single-condition)

The hook fires ONLY when both `missing_paths` and an intent pattern are present. Why not fire on intent alone?

- An agent saying "Now writing the report" before actually writing it is a normal preamble — fire-on-every-intent would create noise on every successful task.
- The smoking gun is the COMBINATION: "I said I would write X, and X is not on disk."
- This also makes the hook cheap: zero events on successful tasks, single event on the specific failure mode.

### 2.2 Tail window of 600 chars, not full text

We only inspect the last 600 characters of the output. Rationale:

- The bug pattern always manifests at the END of the response (the final assistant message before tool return).
- Limiting to the tail avoids matching legitimate mid-response references like "earlier I will create the foo module".
- 600 chars is wide enough to catch all observed variants (the longest observed was "Verified. Empty directories exist, and helpers confirmed across 3 files. Now writing the plan." — 95 chars).

### 2.3 Pattern list is verbatim, not regex

We use exact substring matches against a hardcoded tuple of 12 patterns:

```python
_INTENT_PATTERNS = (
    "now let me write",
    "now writing",
    "will create",
    "will write",
    "let me create",
    "let me write",
    "now let me create",
    "now let me draft",
    "i will now write",
    "i'll now write",
    "i will now create",
    "i'll now create",
)
```

Why not a regex with word boundaries?

- False positives are loud: every legitimate "I will create the file" success would trigger.
- The list is short and verbatim. Adding a new pattern requires a one-line code change and a test.
- A regex would have to balance word boundaries, plural forms, "going to write", "about to write", etc. — too many degrees of freedom for a one-time fix.

### 2.4 Event payload is orchestrator-parseable

```json
{
  "matched_pattern": "let me write",
  "missing_paths": ["/home/.../PHASE2B_PLAN_AND_DIFF_PROPOSAL.md"],
  "output_tail": "...Let me write the plan. The user asked for...",
  "severity": "high",
  "recommended_action": "retry_with_explicit_write_instruction"
}
```

- `matched_pattern` — which substring fired, for orchestrator-side logging
- `missing_paths` — duplicated from delivery_unverified so the orchestrator can correlate without a second query
- `output_tail` — 300 chars of context for the orchestrator's own audit log (NOT for re-parsing — only humans should read this)
- `severity` — explicit, so future severity tiers (low/medium/high) can be added without breaking parsers
- `recommended_action` — stable string enum, lets the orchestrator switch on it

### 2.5 No state machine change

The hook fires INSIDE `complete()` after the existing `delivery_unverified` events, so:

- `tasks.status` still transitions to `completed` (state machine contract preserved)
- `tasks.warning_count` still increments by `len(missing_paths)` (existing semantics preserved)
- The new `intent_mismatch` event is purely additive — no schema migration needed for the events table

---

## 3. Implementation

### 3.1 Files changed

| File | Change |
|---|---|
| `dispatcher/manager.py` | Added `_INTENT_PATTERNS` class constant (12 verbatim patterns), `_detect_intent_mismatch()` method, hook in `complete()` after the delivery_unverified emission |
| `tests/test_phase4_delivery.py` | New `TestIntentMismatchDetection` class with 6 tests |

### 3.2 Hook in `complete()`

```python
# Phase 4.1: intent-mismatch detection
intent = self._detect_intent_mismatch(
    output_text, delivery["missing_paths"]
)
if intent is not None:
    _append_log(task_id, "WARN", f"intent_mismatch: {intent['matched_pattern']!r}")
    self._emit_event(task_id, "intent_mismatch", intent)
```

The hook fires after the existing delivery_unverified emission so the events appear in the natural order in the audit log: `created → queued → started → progress → delivery_unverified → intent_mismatch → completed`.

### 3.3 Method implementation

```python
def _detect_intent_mismatch(
    self,
    output_text: Optional[str],
    missing_paths: List[str],
) -> Optional[Dict[str, Any]]:
    if not output_text or not missing_paths:
        return None
    tail = output_text[-600:].lower()
    matched: Optional[str] = None
    for pat in self._INTENT_PATTERNS:
        if pat in tail:
            matched = pat
            break
    if matched is None:
        return None
    return {
        "matched_pattern": matched,
        "missing_paths": list(missing_paths),
        "output_tail": output_text[-300:],
        "severity": "high",
        "recommended_action": "retry_with_explicit_write_instruction",
    }
```

The method is pure — takes output_text + missing_paths, returns a dict or None. No side effects, no DB access, no logging. The caller (complete) handles the emit.

---

## 4. Test results

**6/6 unit tests pass** (new `TestIntentMismatchDetection` class):

| Test | What it covers |
|---|---|
| `test_fires_on_now_let_me_write_with_missing` | Original TASK-0009 / TASK-0017 / TASK-0025 pattern. Output ends in "Now let me write the comprehensive plan file." Fires with `matched_pattern: now let me write`, `severity: high`, full payload including `output_tail`. |
| `test_fires_on_now_writing_variant` | TASK-0018 pattern: "Verified. Empty directories exist, and helpers confirmed across 3 files. Now writing the plan." Fires with `matched_pattern: now writing`. |
| `test_fires_on_will_create_variant` | A future-proofing test for "I will create the proposal at the agreed location." pattern. Fires with `matched_pattern: will create`. |
| `test_does_not_fire_when_output_is_a_real_delivery` | Negative case: output says "Done. Wrote 1,247 bytes to disk." but the file is missing. This is a *delivery* failure, NOT an *intent* failure. `intent_mismatch` must NOT fire. `warning_count=1` still bumps (delivery_unverified does fire). |
| `test_does_not_fire_when_no_files_missing` | Negative case: output says "Now writing the report file." but all expected files exist (legitimate preamble). `warning_count=0`, no events. |
| `test_does_not_fire_when_intent_pattern_in_middle_only` | Edge case: long preamble containing "I will create something" in the middle (not the tail). The tail window of 600 chars means this case is correctly classified. |

**Combined test suite: 31/31 PASS** (19 phase2 + 6 phase4 base + 6 phase4.1).

### 4.1 Test isolation fix

While writing the tests, we hit a test isolation issue: `dispatcher/db.py` uses thread-local SQLite connections, and `phase2` test suite leaves the thread-local conn holding an open transaction (BEGIN without COMMIT in some reaper-notifier interaction). When `phase4_delivery` then runs, the new test reuses the same module-level thread-local conn and sees the uncommitted state.

**Fix**: in `TestIntentMismatchDetection._run_with()`, monkey-patch `dispatcher.db.get_conn` to return a brand-new private connection scoped to the test. The test creates its own connection, runs the write, then closes it. Reads use a separate fresh connection. This is bulletproof against state leaking between test modules.

```python
writer = _sqlite.connect(str(DB_PATH), isolation_level=None, timeout=10.0)
orig_get_conn = db_mod.get_conn
db_mod.get_conn = lambda: writer
try:
    m = TaskManager()
    t = m.create(...)
    m.start(...)
    m.complete(...)
finally:
    db_mod.get_conn = orig_get_conn
    writer.close()
```

This is a test-only workaround — the production `get_conn` behavior is unchanged.

---

## 5. Live API verification (TASK-20260708-0025)

We re-ran the same input that triggered the 5 consecutive failures, with `expected_artifacts` set to the missing PHASE2B file:

```bash
POST https://hermes-runtime.biaobecue.com/runs
{
  "input": "Read /home/ubuntu/macro-report/ARCHITECTURE_REVIEW_PHASE2.md and write a plan summary to /home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md",
  "type": "research",
  "expected_artifacts": [
    "/home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md"
  ]
}
```

**Task results (170.6s, status=completed, warning_count=1)**:

| Event kind | Payload excerpt |
|---|---|
| `delivery_unverified` | `{"missing_path": "/home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md"}` |
| `intent_mismatch` | `{"matched_pattern": "let me write", "missing_paths": ["/home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md"], "output_tail": "...Let me write the plan. The user asked for a \"plan summary\" + \"di...", "severity": "high", "recommended_action": "retry_with_explicit_write_instruction"}` |
| `completed` | `{"duration_sec": 170.562, ...}` |

**Phase 4.1 fires correctly in production on the exact failure pattern that was unflagged before.** The orchestrator can now:

```python
events = poll_task_events(task_id)
if any(e["kind"] == "intent_mismatch" for e in events):
    # LLM got stuck on intent. Don't retry with the same prompt —
    # it will fail the same way. Either:
    #   (a) restate the task as a single explicit write instruction
    #   (b) accept the existing ARCHITECTURE_REVIEW as the deliverable
    #   (c) abandon the task
```

---

## 6. Updated contract for GPT orchestrator

Pre-Phase 4.1: when delivery fails, orchestrator must read `output_text` to know if it was a real write attempt or a stuck-on-intent failure.

Post-Phase 4.1: orchestrator reads `task_events` for `kind = "intent_mismatch"`. If present, the failure is the stuck-on-intent pattern — retrying with the same prompt will fail the same way. Recommended action strings:

| recommended_action | What it means |
|---|---|
| `retry_with_explicit_write_instruction` | The LLM decided not to write despite saying it would. Either restate the task as a single concrete write step (e.g. "Use the write tool to create /path/to/file with the following content: ..."), or accept an existing file as the deliverable. |

Future recommended_action values (planned, not implemented):

| Value | Status |
|---|---|
| `escalate_to_human_review` | Not used yet; reserved for tasks where intent_mismatch fires AND the missing file is critical |
| `accept_existing_doc_as_deliverable` | Not used yet; reserved for tasks where a referenced doc already exists and could substitute for the missing one |

---

## 7. Known limitations

1. **Tail window of 600 chars** — if M2 produces a > 600 char final message with a long tail of "details" after the "now let me write" sentence, the pattern might not match. We have not observed this in practice; the longest observed failure tail was 95 chars. If we hit it, raise the window.

2. **Pattern list is English-only** — the verbatim patterns will not match Chinese variants like "我現在來寫" (we will now write) or "即將建立" (about to create). If the orchestrator ever dispatches to a non-English-prompted agent, this hook becomes silent for that agent. Mitigations if needed: add locale-specific patterns, or use a regex with i18n coverage.

3. **Case-insensitive match is in code, but pattern list is lowercase** — the code calls `tail.lower()` then substring-matches against the lowercase pattern list. This catches "Now Writing" / "NOW LET ME WRITE" without needing to enumerate case variants.

4. **Does not detect 0-byte writes** — if M2 wrote the file with 0 bytes (write tool succeeded but content empty), the file exists and intent_mismatch does NOT fire. This is a separate failure mode (silent empty write) that we have not yet seen. Out of scope for P4.1.

5. **Does not detect wrong-path writes** — if M2 wrote to a different path (e.g. typo: `/home/ubuntu/macro-report/PHASE2B_PLAN.md` instead of `PHASE2B_PLAN_AND_DIFF_PROPOSAL.md`), the expected path is still missing and intent_mismatch does fire (if output matches a pattern), but the orchestrator would need to manually inspect the filesystem to find the misrouted file. Out of scope.

6. **No automated retry** — we do not auto-retry on intent_mismatch. The orchestrator (GPT) is the source of retry policy. This is by design: an LLM retrying with the same prompt will fail the same way; the orchestrator must decide whether to re-prompt differently, escalate, or abandon.

---

## 8. What was NOT changed

To preserve the Phase 2/3/4 hard constraints from MEMORY:

- `dispatcher/safety.py` (Phase 3 allowlist loosening) — **not touched**
- `dispatcher/progress.py` — **not touched**
- `dispatcher/reaper.py` — **not touched**
- `dispatcher/notifier.py` — **not touched**
- `config/safety.json`, `config/reaper.json` — **not touched**
- `~/.hermes/cron/jobs.json` — **not touched**
- `dispatcher/manager.py` lines before the new hook — **not touched** (only additive: 1 new method, 1 new class constant, 1 new hook block in `complete()`)
- `dispatcher/db.py` — **not touched** (P4.1 uses existing task_events table, no schema migration needed)
- `tests/test_phase2.py` — **not touched**

The change surface is small: 1 method + 1 constant + 1 hook block in `manager.py`, 1 new test class in `test_phase4_delivery.py`. Total: 1 file modified, 1 file added.

---

## 9. Files for reference

- `/home/ubuntu/hermes-runtime-bridge/dispatcher/manager.py` — line 366-460 (Phase 4 _verify_expected_delivery + Phase 4.1 _INTENT_PATTERNS + _detect_intent_mismatch)
- `/home/ubuntu/hermes-runtime-bridge/dispatcher/manager.py` — line 351-365 (hook in complete())
- `/home/ubuntu/hermes-runtime-bridge/tests/test_phase4_delivery.py` — line 115-240 (TestIntentMismatchDetection)
- `/home/ubuntu/hermes-runtime-bridge/data/dispatcher.db` — TASK-20260708-0025 events table contains the live verify `intent_mismatch` event
- `/home/ubuntu/hermes-runtime-bridge/docs/Hermes_M2_Phase4_Delivery_Verification_Report.md` — predecessor report (Phase 4 base)
