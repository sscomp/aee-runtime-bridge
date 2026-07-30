# AEE §21.5 Docker Profiles — Scope Verification & Implementation Blocker Report

**Workorder:** AEE 下一工項 Scope Verification + Implementation（不得預設為 Epic 9.5）
**Date (UTC):** 2026-07-17T10:07:00Z
**Date (CST):** 2026-07-17 18:07 CST
**Executor:** M2 Agent (Hermes, abacusclaw-120fa02af8), glm-5.2 via ollama-cloud
**Repo:** /home/ubuntu/hermes-runtime-bridge
**Verdict:** **FAIL — Scope Verification complete; implementation BLOCKED on missing prerequisite (Docker daemon unavailable)**

---

## 1. Execution Timing

| Phase | Start (UTC) | End (UTC) | Duration |
|---|---|---|---|
| Baseline capture | 2026-07-17T10:00 | 2026-07-17T10:01 | ~1 min |
| Master Plan §21.x read | 2026-07-17T10:01 | 2026-07-17T10:02 | ~1 min |
| Scope Verification (§21.5) | 2026-07-17T10:02 | 2026-07-17T10:03 | ~1 min |
| Prerequisite check (Docker daemon) | 2026-07-17T10:03 | 2026-07-17T10:04 | ~1 min |
| Artifact creation | 2026-07-17T10:04 | 2026-07-17T10:06 | ~2 min |
| Telegram send | 2026-07-17T10:06 | 2026-07-17T10:07 | ~1 min |
| **Total** | | | **~7 min** |

---

## 2. Overall Verdict

**FAIL — Scope Verification complete; implementation BLOCKED on missing prerequisite**

The next formal section after §21.4 in the Master Plan is **§21.5 Docker Profiles** (NOT a presumed "Epic 9.5"; the Master Plan uses §21.x sub-section numbering). Scope was verified as clear and well-defined. However, the §21.5 acceptance criterion #5 (`docker run aee:X.Y.Z --profile {full,mini,edge,developer} works for all four`) requires a running Docker daemon to validate. The Docker daemon is **not available** on this host (`/var/run/docker.sock` missing, `docker info` reports "Cannot connect to the Docker daemon"). Per workorder §3: "若 scope 不清楚、文件矛盾、缺少 prerequisite，停止於 Scope Verification，不得猜測實作" — implementation is halted at Scope Verification. No source code changes were made.

---

## 3. Baseline

| Field | Value |
|---|---|
| Repository | /home/ubuntu/hermes-runtime-bridge |
| Branch | master |
| HEAD | `4207b2feb7bf993ba6df15b8e3f8534e068cb8f4` (matches workorder-stated known commit) |
| git status | 22 untracked files, 0 tracked modifications (see §3.1) |
| Tracked diff stat | (empty — no tracked file modifications) |
| Stash list | `stash@{0}: On master: WIP AEE TASK-20260711-0033 write-side metadata recovery` (1 entry, pre-existing, untouched) |
| Master Plan sha256 | `8c5ca5c1b449c36839ff1887bbadcaafd792659ea66891a6895acb2892cd3d63` |
| Master Plan lines | 7767 |

### 3.1 Untracked files (22, all pre-existing, NOT staged)

```
?? AEE_7_7d_7e_MANIFEST.json
?? AEE_7_7d_7e_STAGING_BOUNDARY.md
?? AEE_7_8_K2_IMPLEMENTATION_REPORT_20260712.md
?? AEE_7_8_K2_PLAN_REPORT_20260712.md
?? Hermes_G3_Write_Investigation_Report_20260712.md
?? TASK-M12_GPT_ACTION_PUBLICATION_READINESS_REVIEW.md
?? TASK-M13_FINAL_PUBLICATION_AND_E2E_VALIDATION.md
?? claude_auth_environment_provisioning_report.md
?? claude_executor_deployment_readiness_review.md
?? claude_executor_post_restart_validation.md
?? data/dispatcher.db.pre-rebuild-20260711T152000Z
?? data/dispatcher.db.pre-rebuild-20260711T152000Z-shm
?? data/dispatcher.db.pre-rebuild-20260711T152000Z-wal
?? executor_router_claude_adapter_manifest_gate_independent_review.md
?? executor_router_manifest_gate_atomic_commit_report.md
?? executor_router_manifest_gate_commit_readiness_review.md
?? k3_atomic_commit_report.md
?? k3_git_validation_report.md
?? k3_independent_review_report.md
?? k3_test_implementation_report.md
?? openapi_auth_env_atomic_commit_report.md
?? openapi_auth_env_independent_review.md
```

All 22 untracked items are pre-existing reports/backups from prior AEE-7.x and AEE-MINI work. None were created or modified by this session. None are staged.

### 3.2 Last 5 commits

```
4207b2f feat(aee): Epic 9.4 §21.4 runtime profile selection — single resolution point
8a43494 feat(aee-9.3): add install.sh shell wrapper with integration tests
e17ff0b feat(aee): Epic 9.3 §21.3 installer backend — Python plan/preflight/dry-run slice
d55b79d feat(aee): Epic 9.2 §21.2 unified CLI UX — install --profile {full,mini,edge,developer}
b1ed56c feat(aee): Epic 9.1 §21.1 canonical product profile matrix
```

### 3.3 Baseline test sanity (read-only, no source changes)

| Suite | Result |
|---|---|
| Epic 9.4 targeted (`test_aee94_runtime_profile_selection`) | 45/45 PASS |
| Epic 9.3 targeted (`test_aee93_installer_backend`) | 54/54 PASS |

Baseline confirmed green for the immediately-prior Epic 9.x slice. No regressions introduced (none could be — no source touched).

---

## 4. Verified Next Epic

### 4.1 Section identification

The Master Plan's chapter §21 ("Epic 9 — Architecture Unification") contains sub-sections §21.1 through §21.10, plus §21.M (Migration Timeline), §21.R (Risks), §21.A (Acceptance Criteria), §21.X (Relationship to AEE-8.x), and §21.Receipt (Append Receipt).

**§21.1** — Product Profile Matrix — SHIPPED (commit `b1ed56c`)
**§21.2** — CLI UX `--profile` flag — SHIPPED (commit `d55b79d`)
**§21.3** — Installer Profile — SHIPPED (commit `e17ff0b` backend + `8a43494` shell wrapper)
**§21.4** — Runtime Profile Selection — SHIPPED (commit `4207b2f`, the verified HEAD)
**§21.5** — Docker Profiles — **NOT SHIPPED** ← next formal section
**§21.6** — Cloud Profiles — NOT SHIPPED
**§21.7** — CI/CD Matrix — NOT SHIPPED
**§21.8** — Release Strategy — NOT SHIPPED
**§21.9** — Documentation Migration — NOT SHIPPED
**§21.10** — Deprecation Plan — NOT SHIPPED

### 4.2 §21.5 Docker Profiles — exact text (Master Plan lines 7626–7630)

> **Purpose:** Single Docker image with profile-specific entrypoints. One image, one codebase, profile selected at `docker run` time.
>
> **Proposal:** One `Dockerfile` at repo root. One image tag per version: `aee:2.0.0` (not `aee-full:2.0.0` + `aee-mini:2.0.0`). Entrypoint accepts `--profile` as first arg: `docker run aee:2.0.0 --profile mini`. `--profile edge` → `AEE_DB_READ_ONLY=1` env var. `--profile developer` → tempdir DB + smoke test + interactive shell.

### 4.3 Acceptance criterion (§21.A #5, line 7704)

> **§21.5** — One Docker image, `docker run aee:X.Y.Z --profile {full,mini,edge,developer}` works for all four. ☐

### 4.4 Dependency on §21.4 (Phase B → Phase C)

Per §21.M Migration Timeline (line 7673–7674):
- Phase B — Install/Runtime: §21.3, §21.4 → `2.0.0-rc1.beta` — **DONE**
- Phase C — Packaging: §21.5, §21.6 → `2.0.0-rc1.gamma` — depends on Phase B (satisfied)

§21.5 builds on §21.4's runtime profile resolution: the Docker entrypoint must accept `--profile` and propagate it through the same single resolution point (`parse_profile` → `Task.profile` → `safety.py:evaluate(profile=...)` → `set_db_profile`). The `--profile edge` → `AEE_DB_READ_ONLY=1` env var mapping connects to §21.4's `set_db_profile("edge")` → `PRAGMA query_only=1` enforcement.

### 4.5 Forbidden scope (cross-epic prohibitions)

Per workorder §4 and §21.M phase isolation:
- §21.6 Cloud Profiles (Terraform) — separate phase
- §21.7 CI/CD Matrix — separate phase
- §21.8 Release Strategy — separate phase
- §21.9 Documentation Migration — separate phase
- §21.10 Deprecation Plan — separate phase
- Any AEE-7.x or AEE-MINI work — out of scope

---

## 5. Scope Assessment

### 5.1 Scope clarity

**CLEAR.** The §21.5 proposal is unambiguous:
1. One `Dockerfile` at repo root
2. One image tag per version (`aee:X.Y.Z`, not per-profile tags)
3. Entrypoint accepts `--profile` as first arg
4. `--profile edge` → `AEE_DB_READ_ONLY=1` env var
5. `--profile developer` → tempdir DB + smoke test + interactive shell
6. Acceptance: `docker run aee:X.Y.Z --profile {full,mini,edge,developer}` works for all four

### 5.2 Compatibility requirements (workorder §5)

The §21.5 slice must preserve compatibility with:
- **Canonical profile matrix** (`aee/profiles/descriptor.py` — `KNOWN_PROFILES`, `DEFAULT_PROFILE`, `parse_profile`)
- **CLI** (`aee/cli.py` — `--profile` flag, `install` subcommand)
- **Installer backend** (`aee/installer/backend.py` — `InstallPlan`, `PreFlightResult`)
- **Shell wrapper** (`install.sh` — delegates to `aee.cli`)
- **Runtime profile selection** (§21.4: `app.py` `POST /runs` → `parse_profile` → `set_db_profile` → `safety.py:evaluate`)

The Docker entrypoint would delegate to `aee.cli` (or `install.sh`) for profile validation, and set `AEE_DB_READ_ONLY=1` for `edge` (which §21.4's `set_db_profile` would read as an alternative signal, OR the entrypoint maps `--profile edge` → calls the same `set_db_profile("edge")` path). No new profile matrix; no CLI rewrite.

### 5.3 Prerequisite check — **BLOCKER**

| Prerequisite | Status | Evidence |
|---|---|---|
| Phase B (§21.3 + §21.4) shipped | ✅ Satisfied | commits `e17ff0b`, `8a43494`, `4207b2f` |
| `aee/profiles/descriptor.py` importable | ✅ Present | `aee/profiles/descriptor.py` exists, `KNOWN_PROFILES` defined |
| `aee/cli.py` install subcommand | ✅ Present | `aee/cli.py` line 119 `install_parser` |
| `install.sh` shell wrapper | ✅ Present | `install.sh` at repo root, 9756 bytes |
| **Docker daemon running** | ❌ **MISSING** | `docker info` → "Cannot connect to the Docker daemon" |
| `/var/run/docker.sock` socket | ❌ **MISSING** | `ls -la /var/run/docker.sock` → "No such file or directory" |
| `docker buildx` plugin | ❌ **MISSING** | `docker buildx version` → "unknown command" |
| `hadolint` linter | ❌ MISSING (non-blocking) | `command -v hadolint` → empty |
| `dockerfile-utils` | ❌ MISSING (non-blocking) | `command -v dockerfile-utils` → empty |

**Docker daemon is the decisive blocker.** The §21.5 acceptance criterion #5 requires `docker run aee:X.Y.Z --profile {full,mini,edge,developer}` to work for all four profiles. Without a running Docker daemon, the image cannot be built, the entrypoint cannot be exercised, and the acceptance criterion cannot be verified.

### 5.4 Docker context investigation

```
NAME         DESCRIPTION                               DOCKER ENDPOINT
default      Current DOCKER_HOST based configuration   unix:///var/run/docker.sock
home-mac *                                             ssh://home-ssh
rootless     Rootless mode                             unix:///home/ubuntu/.docker/run/docker.sock
```

The active context is `home-mac` (points to `ssh://home-ssh`, an SSH-tunneled Docker daemon on a remote Mac). This context is not reachable from the Abacus container (no SSH key/config for `home-ssh`, and the Abacus container blocks inbound connections). The `default` context points to `/var/run/docker.sock` which does not exist. The `rootless` context points to `/home/ubuntu/.docker/run/docker.sock` which also does not exist.

### 5.5 Why implementation is halted (not faked)

Per workorder §3: "若 scope 不清楚、文件矛盾、缺少 prerequisite，停止於 Scope Verification，不得猜測實作".

The scope IS clear, but the **prerequisite** (Docker daemon) is missing. Writing a `Dockerfile` without the ability to build or run it would be "guessing implementation" — the acceptance criterion (`docker run ... works for all four`) cannot be verified. Shipping a `Dockerfile` + tests that cannot be executed against a real Docker daemon would violate workorder §7 ("應執行測試而未執行... 必須 FAIL") and the SOUL.md rule: "NEVER substitute plausible-looking fabricated output for results you couldn't actually produce."

The honest path is: **report the blocker, halt, await user direction on how to proceed** (e.g., user starts a Docker daemon, user authorizes a "Dockerfile-only without build verification" slice, or user redirects to a different work item).

---

## 6. Change Summary

**No source code changes were made.** This was a read-only Scope Verification session.

| File | Change |
|---|---|
| (none) | (no source files modified, no test files added, no Dockerfile created) |

The only artifact created is this report (durable artifact, per workorder §9).

---

## 7. Evidence

### 7.1 Git evidence

```
HEAD: 4207b2feb7bf993ba6df15b8e3f8534e068cb8f4
Branch: master
Tracked diff: (empty)
Stash: stash@{0}: On master: WIP AEE TASK-20260711-0033 write-side metadata recovery
Untracked: 22 files (all pre-existing reports/backups, none created this session)
```

### 7.2 Master Plan evidence

```
File: /home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md
SHA-256: 8c5ca5c1b449c36839ff1887bbadcaafd792659ea66891a6895acb2892cd3d63
Lines: 7767
§21.5 location: lines 7626–7630
§21.A #5 location: line 7704
§21.M Phase C location: line 7674
```

### 7.3 Docker daemon evidence

```
$ docker --version
Docker version 29.4.3, build 055a478

$ docker info 2>&1 | head -10
Client: Docker Engine - Community
 Version:    29.4.3
 Context:    home-mac
 Debug Mode: false
 Plugins:
  compose: Docker Compose (Docker Inc.)
    Version:  v5.1.3
    Path:     /usr/libexec/docker/cli-plugins/docker-compose

Server:
Cannot connect to the Docker daemon at http://docker.example.com. Is the docker daemon running?

$ ls -la /var/run/docker.sock /run/docker.sock
ls: cannot access '/var/run/docker.sock': No such file or directory
ls: cannot access '/run/docker.sock': No such file or directory

$ docker buildx version
docker: unknown command: docker buildx

$ docker context ls
NAME         DOCKER ENDPOINT
default      unix:///var/run/docker.sock       (socket missing)
home-mac *   ssh://home-ssh                   (unreachable from Abacus container)
rootless     unix:///home/ubuntu/.docker/run/docker.sock  (socket missing)
```

### 7.4 Baseline test evidence (read-only sanity)

```
Epic 9.4 targeted (aee.tests.test_aee94_runtime_profile_selection): 45/45 PASS
Epic 9.3 targeted (aee.tests.test_aee93_installer_backend): 54/54 PASS
```

### 7.5 Compatibility surface evidence

```
aee/profiles/descriptor.py: KNOWN_PROFILES = ("full","mini","edge","developer"), DEFAULT_PROFILE = "full"
aee/cli.py: install subcommand with --profile flag (line 119)
aee/installer/backend.py: InstallerBackend, InstallPlan, PreFlightResult (20004 bytes)
install.sh: shell wrapper delegating to aee.cli (9756 bytes)
app.py: POST /runs → parse_profile → set_db_profile → safety.evaluate (§21.4)
dispatcher/db.py: set_db_profile/get_db_profile + _apply_pragmas PRAGMA query_only=1 for edge
requirements.txt: fastapi, uvicorn[standard], httpx, python-dotenv, pydantic
```

---

## 8. Tests

| Suite | Run? | Result | Baseline failure? |
|---|---|---|---|
| Epic 9.4 targeted | Yes (sanity) | 45/45 PASS | N/A |
| Epic 9.3 targeted | Yes (sanity) | 54/54 PASS | N/A |
| Epic 9.5 targeted | N/A — no §21.5 implementation exists | N/A | N/A |
| Impacted regression | Not run — no source changes to regress | N/A | N/A |
| Broad regression | Not run — no source changes to regress | N/A | N/A |
| Docker build/run acceptance | **NOT RUN — Docker daemon unavailable** | **BLOCKED** | N/A |

No tests were skipped that should have been run. The Docker build/run acceptance test for §21.5 cannot be executed because the prerequisite (Docker daemon) is missing. This is the root cause of the FAIL verdict.

---

## 9. Regression

No regression suite was run because no source code was modified. The baseline sanity checks (Epic 9.4 + 9.3 targeted) confirm the pre-implementation state is green. No regressions can be attributed to this session.

---

## 10. Artifact Verification

### 10.1 This report (durable artifact)

```
File: /home/ubuntu/hermes-runtime-bridge/AEE_9_5_DOCKER_PROFILES_SCOPE_VERIFICATION_REPORT_20260717.md
Naming: per workorder §9, named after the Master Plan's formal Epic name (§21.5 Docker Profiles)
ls -la: -rw------- 1 ubuntu ubuntu 21717 Jul 17 18:08
wc -l: 443
sha256sum: c5e1eeb31e8d8d689a4b7201fc34850713728f034799c93289826b58c779a80e
Note: sha256 is self-referential (updating it changes it); the recorded value is the sha of the file before this sha line was last edited. Final on-disk sha will differ by exactly the bytes of this edit. This is the same self-referential sha recursion noted in Master Plan §21.Receipt (line 7751).
```

### 10.2 Required headings check

| Required heading (workorder §13) | Present? |
|---|---|
| Execution Timing | ✅ §1 |
| Overall Verdict | ✅ §2 |
| Baseline | ✅ §3 |
| Verified Next Epic | ✅ §4 |
| Scope Assessment | ✅ §5 |
| Change Summary | ✅ §6 |
| Evidence | ✅ §7 |
| Tests | ✅ §8 |
| Regression | ✅ §9 |
| Artifact Verification | ✅ §10 |
| Production Safety | ✅ §11 |
| Remaining Risks | ✅ §12 |
| Review Ready | ✅ §13 |
| Commit Ready | ✅ §14 |
| Telegram | ✅ §15 |

All 15 required headings verified present (auto-checked via Python script).

---

## 11. Production Safety

| Control | Status |
|---|---|
| No commit | ✅ No `git commit` was run |
| No push | ✅ No `git push` was run |
| No deploy | ✅ No deploy/restart was run |
| No stash | ✅ No `git stash` (the pre-existing `stash@{0}` was not touched) |
| No merge | ✅ No `git merge` was run |
| No rebase | ✅ No `git rebase` was run |
| No source modification | ✅ Zero tracked file modifications |
| No Master Plan modification | ✅ Master Plan sha256 unchanged (`8c5ca5c1...`) |
| No supervisord reload | ✅ No supervisord interaction |
| No DB writes | ✅ No DB access; `dispatcher.db` untouched |
| No Docker daemon start | ✅ Did not attempt to start a Docker daemon (would be a system-level change requiring user authorization) |
| No env file writes | ✅ No `.env` modification |
| No new untracked files (other than this report) | ✅ The 22 untracked files are pre-existing |

The 5 red lines from workorder §8 are all honored.

---

## 12. Remaining Risks

| ID | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | Docker daemon unavailable blocks §21.5 implementation | High | Surface to user; await direction (start daemon / authorize Dockerfile-only slice / redirect work) |
| R2 | `home-mac` Docker context points to unreachable SSH endpoint | Medium | User may need to switch context (`docker context use default`) after starting a local daemon, or provision SSH access to `home-ssh` |
| R3 | `docker buildx` plugin missing — modern Dockerfiles often use BuildKit features | Low | Can fall back to legacy builder (`docker build`) or install buildx after daemon is up |
| R4 | §21.5 acceptance criterion requires all 4 profiles to work via `docker run` — `developer` profile's "tempdir DB + smoke test + interactive shell" is the most complex entrypoint mode | Medium | Implementation plan (deferred) should address `developer` profile entrypoint carefully |
| R5 | Pre-existing `stash@{0}` from AEE-7.11 write-side metadata recovery remains in the repo | Low | Not touched by this session; flagged for awareness only |
| R6 | 22 untracked report/backup files clutter the working tree | Low | Not created by this session; cleanup is a separate housekeeping item |
| R7 | If user authorizes a "Dockerfile-only without build verification" slice, the acceptance criterion #5 cannot be fully met — would be a partial ship requiring re-verification once Docker is available | Medium | Surface this tradeoff explicitly if user considers that path |

---

## 13. Review Ready

**Review Ready: YES (for Scope Verification portion only)**

The Scope Verification is complete and reviewable:
- Next formal section identified: §21.5 Docker Profiles (lines 7626–7630)
- Exact requirements and acceptance criteria quoted
- Dependency on §21.4 (Phase B → Phase C) confirmed satisfied
- Forbidden scope enumerated
- Prerequisite blocker (Docker daemon) documented with evidence

**Review Ready: NO (for Implementation portion)**

No implementation was produced. The implementation portion cannot be review-ready because it does not exist. This is the correct outcome per workorder §3 (halt on missing prerequisite).

---

## 14. Commit Ready

**Commit Ready: NO**

Nothing to commit. No source code changes were made. The only artifact is this report (a durable artifact, intentionally untracked per AEE convention — reports are not committed to the bridge repo without explicit user direction).

Git state is unchanged from the start of the session:
- HEAD: `4207b2feb7bf993ba6df15b8e3f8534e068cb8f4` (same as start)
- Tracked diff: empty
- Stash: 1 entry (pre-existing, untouched)
- Untracked: 22 files (pre-existing) + this report

---

## 15. Telegram

Per USER PROFILE `AEE-MINI Telegram rule strengthened (2026-07-13)`: all AEE-MINI workorders must attempt Telegram notification to 鼎鼎 regardless of outcome. This workorder is an AEE next-step scope verification (not strictly AEE-MINI), but the §21.5 scope concerns the unified product line that subsumes AEE-MINI. Per the strengthened rule and the workorder §12 requirement, a Telegram notification will be attempted.

**Planned message (short version per Telegram 派工回報格式偏好):**

```
✅ AEE §21.5 Scope Verification (BLOCKED)
訊息類型: 15-section Scope Verification Report
開始: 2026-07-17 18:00 CST
結束: 2026-07-17 18:05 CST
耗時: ~5 min
HEAD: 4207b2f (unchanged)
Verdict: FAIL — Scope clear, implementation BLOCKED on missing Docker daemon
下一工項: §21.5 Docker Profiles (Master Plan lines 7626-7630)
Blocker: docker daemon 不在運行 (/var/run/docker.sock missing)
Test count: 45/45 + 54/54 baseline sanity PASS (no source changes)
Commit SHA: N/A (no commit)
完整報告: /home/ubuntu/hermes-runtime-bridge/AEE_9_5_DOCKER_PROFILES_SCOPE_VERIFICATION_REPORT_20260717.md
```

Telegram send will be attempted via `hermes send` after this report is written to disk. The `message_id` (if successful) will be recorded here:

**Telegram send result:** **SUCCESS** — `message_id: 7454`, `chat_id: 5132341473` (鼎鼎), `mirrored: true`, sent 2026-07-17 18:08 CST via `hermes send --to telegram:5132341473 --file /tmp/aee95_telegram_msg.txt --json`.

---

*End of §21.5 Scope Verification Report. Implementation halted per workorder §3 (missing prerequisite: Docker daemon). Awaiting user direction.*