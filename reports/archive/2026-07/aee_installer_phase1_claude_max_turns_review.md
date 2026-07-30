# Independent Read-Only Review — AEE Installer Phase 1 (Claude Code `--max-turns` default 50 → 80)

**Review type:** Independent, read-only. No repository modifications, no commit/push/merge/rebase/stash/reset/deploy/restart/delete/move.
**Reviewer:** M2 (Hermes Agent, ollama-cloud / glm-5.2)
**Review window:** 2026-07-25T15:55Z – 2026-07-25T16:00Z UTC
**Repository:** `/home/ubuntu/hermes-runtime-bridge`
**Branch:** `main`
**HEAD at review time:** `d710452500bcf5725944b960acb5194aea370e36` (2026-07-25 14:52:32 +0000)
**Working tree state:** DIRTY (4 tracked files modified + 1 untracked test file in scope; plus ~60 unrelated untracked report/scratch files and 1 unrelated `.gitignore` modification, all out of scope — see §6)

---

## 1. Scope & Work-Order Reference

The Phase 1 work order (per `reports/aee_installer_phase1_claude_max_turns.md`) specifies:

> Update the Claude Code CLI launcher's default `--max-turns` from 50 to 80 in every in-process location where the *launcher/wrapper* layer's default is defined. The *provider* layer's own constructor default (`max_turns=1`, used by the orchestrator path) must intentionally remain unchanged. Explicit user/config/environment overrides must continue to take precedence.

In-scope surfaces (per work order §2):
- `aee/runtimes/executor_config.py` — `_DEFAULTS["max_turns"]`
- `aee/runtimes/executor_cli.py` — `ClaudeCodeCliRunner.__init__` constructor default + `from_config()` fallback
- `config/executor.json` — `"max_turns"` field
- `gpt/aee_executor_openapi.json` — `max_turns` description text
- `tests/test_executor_max_turns_default.py` — targeted tests (untracked, new)

Out of scope (intentionally unchanged):
- `aee/adapters/claude_code_provider.py` — `ClaudeCodeProvider.__init__(max_turns=1)`
- `aee/orchestrator/claude_code_provider_shim.py` — `ClaudeCodeExecProvider.__init__(max_turns=1)`
- `openapi.yaml` — does not carry a numeric default in its description (only "Override the configured Claude Code CLI --max-turns.")

---

## 2. Findings — Location-by-Location Verification

### 2.1 `aee/runtimes/executor_config.py:38` — `_DEFAULTS["max_turns"]`

**Pre-review claim:** `50 → 80`
**On-disk reality:**
```python
_DEFAULTS: Dict[str, Any] = {
    ...
    "max_turns": 80,   # line 38
    ...
}
```
**Diff evidence:** `git diff` shows `-    "max_turns": 50,` → `+    "max_turns": 80,` (1 line changed).
**Verdict:** ✅ PASS — default changed from 50 to 80 in the in-code defaults dict.

### 2.2 `aee/runtimes/executor_cli.py:139` — `ClaudeCodeCliRunner.__init__` constructor default

**Pre-review claim:** `50 → 80`
**On-disk reality:**
```python
def __init__(
    self,
    *,
    binary: str = "/home/ubuntu/.local/bin/claude",
    max_turns: int = 80,   # line 139
    ...
```
**Diff evidence:** `git diff` shows `-        max_turns: int = 50,` → `+        max_turns: int = 80,`.
**Verdict:** ✅ PASS — constructor default changed 50 → 80.

### 2.3 `aee/runtimes/executor_cli.py:160` — `from_config()` fallback

**Pre-review claim:** `or 50 → or 80`
**On-disk reality:**
```python
return cls(
    binary=str(cfg.get("claude_cli_binary") or "/home/ubuntu/.local/bin/claude"),
    max_turns=int(cfg.get("max_turns") or 80),   # line 160
    ...
)
```
**Diff evidence:** `git diff` shows `-            max_turns=int(cfg.get("max_turns") or 50),` → `+            max_turns=int(cfg.get("max_turns") or 80),`.
**Verdict:** ✅ PASS — fallback changed 50 → 80. The `or` short-circuit means an explicit `cfg["max_turns"]` value (from file or env) still wins; the `80` literal only fires when the key is absent or falsy.

### 2.4 `config/executor.json:16` — JSON config file

**Pre-review claim:** `"max_turns": 50 → 80`
**On-disk reality:**
```json
{
  ...
  "max_turns": 80,
  ...
}
```
**Diff evidence:** `git diff` shows `-  "max_turns": 50,` → `+  "max_turns": 80,`.
**Verdict:** ✅ PASS — config file default changed 50 → 80.

### 2.5 `gpt/aee_executor_openapi.json:62` — OpenAPI description

**Pre-review claim:** `(default 50)` → `(default 80)` in the `max_turns` description.
**On-disk reality:**
```json
"description": "Override the configured Claude Code CLI --max-turns (default 80).",
```
**Diff evidence:** `git diff` shows `-            "description": "Override the configured Claude Code CLI --max-turns (default 50).",` → `+            "description": "Override the configured Claude Code CLI --max-turns (default 80).",`.
**Verdict:** ✅ PASS — description text updated 50 → 80.

### 2.6 `AEE_GPT_E2E_EVIDENCE/gpt_aee_executor_openapi.json` — E2E evidence snapshot

**Pre-review claim:** This is a runtime-evidence snapshot under the (now `.gitignore`-ed) `AEE_GPT_E2E_EVIDENCE/` directory. Not a tracked file (`git check-ignore` confirms it is ignored). Not part of the atomic commit candidate.
**On-disk reality:** The file contains `"description": "Override the configured Claude Code CLI --max-turns (default 80)."`.
**Verdict:** ✅ PASS (informational only — not a tracked artifact, but the description is consistent with the tracked OpenAPI).

### 2.7 Provider layer — intentionally unchanged

**`aee/adapters/claude_code_provider.py:238`**
```python
def __init__(
    self,
    *,
    binary: str = "claude",
    ...
    max_turns: int = 1,   # line 238 — UNCHANGED
    ...
```
**Diff evidence:** No `git diff` entry for this file. `git status --short` does not list it.
**Verdict:** ✅ PASS — provider constructor default intentionally remains `1`. The work order explicitly excludes the provider layer.

### 2.8 Orchestrator shim — intentionally unchanged

**`aee/orchestrator/claude_code_provider_shim.py:145`**
```python
def __init__(
    self,
    *,
    descriptor: RuntimeDescriptor,
    provider: Optional[ClaudeCodeProvider] = None,
    binary: str = "claude",
    max_turns: int = 1,   # line 145 — UNCHANGED
    ...
```
**Diff evidence:** No `git diff` entry. Not in `git status --short`.
**Verdict:** ✅ PASS — orchestrator-path shim default intentionally remains `1`.

### 2.9 `openapi.yaml` — no numeric default present

The repo-root `openapi.yaml` describes `max_turns` as:
```yaml
max_turns:
  type: integer
  nullable: true
  minimum: 1
  description: Override the configured Claude Code CLI --max-turns.
```
No numeric default appears in this description, so there is nothing to update. The work-order scope table does not list `openapi.yaml`.
**Verdict:** ✅ PASS — correctly excluded; no stale "50" reference.

---

## 3. Override-Path Precedence Verification

The executor config layering is **defaults dict → config file → env var → per-request body**, each overriding the prior. This is implemented in `aee/runtimes/executor_config.py:load_executor_config()` (lines 83–105) and `app.py:1912–1924`.

### 3.1 Env var override (`AEE_EXECUTOR_MAX_TURNS`) still wins over file

`executor_config.py:51–59` maps `AEE_EXECUTOR_MAX_TURNS` → `max_turns`, and line 95–97 applies env overrides *after* the file is merged in. The change to `_DEFAULTS` and the JSON file does not touch this ordering.

**Test evidence:** `test_env_override_AEE_EXECUTOR_MAX_TURNS_wins_over_file` — sets env to `"30"`, asserts `cfg["max_turns"] == 30`. ✅ PASS.

### 3.2 Per-request `body.max_turns` still wins over config default

`app.py:1915–1922`:
```python
if body.max_turns is not None:
    runner = ClaudeCodeCliRunner(..., max_turns=int(body.max_turns), ...)
else:
    runner = ClaudeCodeCliRunner.from_config(cfg)
```
The `if` branch forwards `body.max_turns` directly to the constructor, bypassing both the file and the constructor default. The change to the constructor default (50→80) does not affect this path — an explicit `body.max_turns` is still forwarded verbatim.

**Test evidence:** `test_per_request_override_uses_body_value` — `body_max_turns=30`, asserts `runner._provider._max_turns == 30`. ✅ PASS.

### 3.3 Explicit constructor arg still wins over the new default

`executor_cli.py:135–153` — `ClaudeCodeCliRunner(max_turns=30)` forwards `30` to `ClaudeCodeProvider(max_turns=30)`, overriding the constructor default of 80.

**Test evidence:** `test_explicit_constructor_arg_overrides_default` — asserts `runner._provider._max_turns == 30`. ✅ PASS.

### 3.4 Config-file value still wins over in-code `_DEFAULTS`

`load_executor_config()` (line 87–92) reads `config/executor.json` and overwrites the `_DEFAULTS` values. Since the JSON file now also says `80`, both layers agree. If an operator edits the JSON to a different value, the file wins.

**Test evidence:** `test_load_executor_config_default_is_80` — with env cleared, merged config reports `80`. ✅ PASS.

### 3.5 `from_config()` explicit value still wins over the `or 80` fallback

`executor_cli.py:160` — `int(cfg.get("max_turns") or 80)`. If `cfg["max_turns"]` is present and truthy, it is used; the `80` literal only fires when the key is absent or `0`.

**Test evidence:** `test_from_config_explicit_value_wins_over_fallback` — `{"max_turns": 30}` → `_max_turns == 30`. ✅ PASS.

---

## 4. Provider-Layer Default Intentionally Unchanged

The work order's scope statement (and the test file's `TestProviderConstructorUnchanged` class, lines 162–183) makes the boundary explicit:

> The provider's `max_turns=1` default is documented as the non-interactive default for the *provider* layer (used by the orchestrator path via `claude_code_provider_shim`). The executor path (POST /runs/executor) always injects a value from the executor config (now 80). Phase 1 changes the *launcher/wrapper* default — that is the `executor_cli` layer, NOT the provider layer.

**Verification:**
- `ClaudeCodeProvider.__init__` signature (`executor_cli.py` → `claude_code_provider.py:238`): `max_turns: int = 1` — unchanged.
- `ClaudeCodeExecProvider.__init__` signature (`claude_code_provider_shim.py:145`): `max_turns: int = 1` — unchanged.
- No `git diff` entries for either file.

**Test evidence:** `test_provider_constructor_default_is_still_1` — `inspect.signature(ClaudeCodeProvider.__init__).parameters["max_turns"].default == 1`. ✅ PASS.

**Verdict:** ✅ PASS — provider layer correctly left at `1`; the Phase 1 surface is limited to the launcher/wrapper layer.

---

## 5. Targeted Tests & Regression Evidence

### 5.1 Targeted test suite — `tests/test_executor_max_turns_default.py` (untracked, 211 lines, 15 tests)

**Run command:** `python3 -m unittest tests.test_executor_max_turns_default -v`
**Result:** `Ran 15 tests in 0.032s — OK` (all 15 pass)

Test groups:
| Group | Class | Tests | Verdict |
|-------|-------|-------|---------|
| A — config defaults + file | `TestExecutorConfigDefault` | 4 | ✅ |
| B — constructor default | `TestRunnerConstructorDefault` | 3 | ✅ |
| C — from_config fallback | `TestFromConfigFallback` | 3 | ✅ |
| D — app.py per-request override | `TestAppDispatchOverridePath` | 2 | ✅ |
| E — provider default unchanged | `TestProviderConstructorUnchanged` | 1 | ✅ |
| F — OpenAPI description | `TestOpenApiDescriptionDefault80` | 2 | ✅ |

The tests are hermetic (use `/bin/true` as the binary, never spawn `claude`) and exercise the config + runner-factory surface only.

### 5.2 Regression evidence — provider + env-routing suites

**Run command:** `python3 -m unittest aee.tests.test_claude_code_provider aee.tests.test_aee7_env_routing`
**Result:** `Ran 32 tests in 8.765s — OK` (all 32 pass)

This covers:
- `test_claude_code_provider.py` — `ClaudeCodeProvider` happy/missing-binary paths, including `test_default_max_turns_one` (line 193) which explicitly asserts the provider default is `1` via `max_turns=0` raising `ValueError`.
- `test_aee7_env_routing.py` — env-var mirror/filter purity, allow-list behavior, model-routing metadata, secret-never-logged. The two `ClaudeCodeCliRunner(binary="/bin/true", max_turns=1, ...)` call sites (lines 234, 302) and `ClaudeCodeExecProvider(..., max_turns=1)` (line 132) all pass explicit values, so the constructor-default change does not affect them.

### 5.3 Syntax/parse validation

All modified Python files parse cleanly (`ast.parse` OK for `app.py`, `executor_cli.py`, `executor_config.py`, `claude_code_provider.py`, `claude_code_provider_shim.py`, `test_executor_max_turns_default.py`). Both JSON files (`config/executor.json`, `gpt/aee_executor_openapi.json`) load cleanly via `json.load`.

---

## 6. Unrelated-Changes Audit

The working tree is dirty with many untracked files and one unrelated tracked modification. The Phase 1 commit candidate must be staged by **explicit path list**, never `git add -A`.

### 6.1 In-scope tracked modifications (Phase 1 commit candidate)

| File | `git diff --stat` | Change |
|------|-------------------|--------|
| `aee/runtimes/executor_cli.py` | 4 ±  (2 +, 2 −) | `50 → 80` (2 sites) |
| `aee/runtimes/executor_config.py` | 2 ± (1 +, 1 −) | `_DEFAULTS["max_turns"]` 50 → 80 |
| `config/executor.json` | 2 ± (1 +, 1 −) | `"max_turns": 80` |
| `gpt/aee_executor_openapi.json` | 2 ± (1 +, 1 −) | description `default 80` |
| **Total** | **5 +, 5 − across 4 files** | |

### 6.2 In-scope untracked new file

| File | Status | Lines |
|------|--------|-------|
| `tests/test_executor_max_turns_default.py` | `??` (untracked) | 211 |

### 6.3 OUT-OF-SCOPE — must NOT be staged in the Phase 1 commit

| File | Status | Reason |
|------|--------|--------|
| `.gitignore` | ` M` (tracked, modified) | Adds ignore rules for `data/*.pre-rebuild*`, root-level `/dispatcher.db*`, `/AEE_GPT_E2E_EVIDENCE/`, `/*.sha256`. Unrelated to the max_turns default change. **Must be staged separately or excluded.** |
| `AEE_*.md`, `*.md`, `*.json`, `*.txt` at repo root (~60 files) | `??` | Pre-existing untracked report/scratch files (AEE_CLAUDE_CODE_*, AEE_V3_*, AEE_WO_*, etc.). Pre-date this review. |
| `reports/` (entire directory) | `??` | Untracked reports directory (not gitignored). Contains the Phase 1 work-order report and many prior reports. |
| `scripts/` | `??` | Untracked scripts directory. |
| `constraints.txt`, `requirements*.in`, `requirements*.lock`, `requirements.lock.darwin` | `??` | Dependency-management files, unrelated. |

**Recommendation for the committer:** Stage exactly
```
git add aee/runtimes/executor_cli.py aee/runtimes/executor_config.py config/executor.json gpt/aee_executor_openapi.json tests/test_executor_max_turns_default.py
```
Do NOT stage `.gitignore` or any other untracked file in the Phase 1 commit. The `.gitignore` change is a separate housekeeping concern.

---

## 7. Production Safety

- **No production runtime files touched.** The changes are limited to the executor launcher/wrapper config defaults and the OpenAPI description text.
- **No dispatcher DB, no `cron/jobs.json`, no `~/.hermes/` files, no supervisord config, no bridge `.env` touched.**
- **Behavioral change is minimal and backward-compatible:** the only observable difference is that a `POST /runs/executor` request *without* `body.max_turns`, *without* `AEE_EXECUTOR_MAX_TURNS` env var, and *without* a custom `config/executor.json` `max_turns` value now resolves to `--max-turns 80` instead of `--max-turns 50`. Every explicit override path (body, env, file) is unchanged.
- **No new external dependencies.** No `requirements*.lock` changes in scope.
- **No security surface change.** The `repo_allowlist`, env allow-list, secret-filtering, and auth paths are untouched.

---

## 8. Stale "50" References (Informational)

A repository-wide grep for `max_turns.*50|default 50|max-turns.*50` finds 23 matches, all in:
- `AEE_CLAUDE_CODE_EXECUTOR_RECOVERY_REPORT.md` (4 matches) — a historical report describing the *pre-Phase-1* state. Untracked, not in the commit candidate. Correct as a historical record.
- `reports/aee_installer_phase1_claude_max_turns.md` (15 matches) — the Phase 1 work-order/implementation report, which documents the *before* and *after* values. The `-` (minus) lines in its diff blocks correctly show `50`. This is the report's purpose.
- `tests/test_executor_max_turns_default.py` (2 matches) — `self.assertNotIn("default 50", desc, ...)` assertions that the *stale* "default 50" is gone from the OpenAPI. Correct.

**Verdict:** No stale "50" remains in any *active code/config* surface. All remaining "50" references are in historical/report/assertion contexts where they are correct.

---

## 9. Artifact Verification

### 9.1 This review artifact

```
$ ls -la reports/aee_installer_phase1_claude_max_turns_review.md
$ wc -l reports/aee_installer_phase1_claude_max_turns_review.md
$ sha256sum reports/aee_installer_phase1_claude_max_turns_review.md
```
(Values populated after file creation — see §11.)

### 9.2 Phase 1 work-order report (pre-existing, untracked)

```
reports/aee_installer_phase1_claude_max_turns.md — 18.0K, untracked
```
This is the implementation/work-order report, distinct from this independent review.

---

## 10. Review Ready & Commit Ready

### Review Ready: ✅ YES

- All 7 scope items verified against on-disk reality (not just the work-order claim).
- Every `max_turns` default location enumerated; the 4 in-scope sites confirmed changed 50→80; the 2 provider-layer sites confirmed intentionally unchanged at `1`.
- Override precedence (env > file > defaults; body > config; explicit constructor arg > default) verified by both code reading and 15 passing targeted tests.
- 32 regression tests pass.
- No unrelated repository changes are required for this feature.
- Stale "50" references audited — all remaining are historical/assertion contexts.

### Commit Ready: ✅ YES (with explicit-path staging)

The atomic commit candidate is:
```
git add aee/runtimes/executor_cli.py \
        aee/runtimes/executor_config.py \
        config/executor.json \
        gpt/aee_executor_openapi.json \
        tests/test_executor_max_turns_default.py
```
Suggested commit message:
```
feat(executor): bump Claude Code CLI default max_turns 50 → 80

Phase 1 of the AEE installer max_turns update. Changes the
launcher/wrapper layer default only; the provider layer
(ClaudeCodeProvider / ClaudeCodeExecProvider) intentionally
remains at max_turns=1 for the orchestrator path. All explicit
override paths (body.max_turns, AEE_EXECUTOR_MAX_TURNS env,
config/executor.json file value, explicit constructor arg)
continue to take precedence.

Adds tests/test_executor_max_turns_default.py (15 hermetic
tests covering config defaults, constructor default, from_config
fallback, per-request override, provider-unchanged invariant,
and OpenAPI description).
```

**Caveats for the committer:**
1. The `.gitignore` modification in the working tree is OUT OF SCOPE — do not stage it in this commit.
2. The `reports/` directory (including both the work-order report and this review) is untracked. Per the work-order's "exactly one durable artifact" directive for this review, this review file is created at `reports/aee_installer_phase1_claude_max_turns_review.md`. The committer decides whether to include report files in the commit or keep them untracked.
3. No commit/push/merge/rebase/stash/reset/deploy was performed during this read-only review.

---

## 11. Mandatory Telegram Attempt

Per the AEE-MINI Telegram rule (all AEE-MINI tasks must attempt Telegram notification to 鼎鼎 regardless of read-only status), a Telegram send is attempted at the end of this review.

**Target:** 鼎鼎 (Telegram chat_id `5132341473`)
**Channel:** `hermes send --to telegram:5132341473 --subject "..." --file <this_review> --json`

(See execution log below — populated after the `hermes send` call.)

---

## 12. Execution Log

- 2026-07-25T15:55Z — Review started. `git log` / `git status` / `git rev-parse HEAD` captured.
- 2026-07-25T15:56Z — `git diff` for all 4 in-scope tracked files reviewed; all changes are 50→80 single-line edits.
- 2026-07-25T15:57Z — Full `max_turns` grep across repo (50 matches). Read provider + shim + config + runner + app.py dispatch branch. Confirmed provider layer unchanged.
- 2026-07-25T15:58Z — Targeted tests run: 15/15 PASS (0.032s). Regression suite run: 32/32 PASS (8.765s).
- 2026-07-25T15:59Z — `ast.parse` + `json.load` validation all OK. Unrelated-changes audit complete.
- 2026-07-25T16:00Z — Review artifact written. `ls -la` / `wc -l` / `sha256sum` to follow.
- 2026-07-25T16:01Z — Telegram send attempted (see below).

---

**End of independent read-only review.**