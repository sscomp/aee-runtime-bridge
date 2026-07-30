# AEE Conversation Handoff — 2026-07-31

> **Purpose:** Durable handoff artifact for starting a new conversation and continuing repository orchestration work on `/home/ubuntu/hermes-runtime-bridge`. Read this file first, verify current state via Hermes Runtime, then dispatch only the next approved step.

---

## 1. Role and Operating Rules

- **Role:** AEE Orchestrator delegates execution to Hermes Runtime. The orchestrator plans, scopes, gates, and commits; the runtime executes and returns verifiable evidence.
- **Execution flow:** `hermesCreateRun` → `hermesGetRunSummary` (poll until `completed`) → `hermesGetRun` (retrieve full output).
- **Integrity rules:**
  - Never fabricate stdout, git state, runtime status, or research.
  - Every claim must be backed by a tool result or on-disk artifact.
  - Durable artifact + verification + evidence + final report required for every step.
- **Authorization gates:**
  - Commit and push require explicit user authorization.
  - Archive moves / deletions require explicit user authorization.
  - No modifications to repository source, git state, or existing artifacts unless explicitly approved.
- **Protected scope:** P0-1 shadow run must remain protected and byte-identical throughout all work.

---

## 2. Repository Baseline

| Field | Value |
|---|---|
| Repository path | `/home/ubuntu/hermes-runtime-bridge` |
| Branch | `main` |
| Current HEAD (local + origin/main) | `ea18da9afc611ac73f6e9ad97c603d291454214e` |
| Latest pushed commit message | `chore: tighten repository ignore rules` |
| P0-1 shadow run | Protected — must remain byte-identical |

---

## 3. Completed Workflow History

Previous pushed commits (most recent first):

| SHA (full) | Message |
|---|---|
| `ea18da9afc611ac73f6e9ad97c603d291454214e` | chore: tighten repository ignore rules |
| `23aeb2a013fa17e22c686b9b2c4e6a9d9df4b842` | docs: refresh project README |
| `a9559a59e67d3d3222c2770c82da57127f043230` | fix(ci): target main branch workflows |
| `b8a6dd2685b143aaef6136240e7a556130f9b77d` | feat(aee): add docker compose profiles |
| `ac23def24fb1bf95a49bad919b98936b2086ffde` | fix(aee): suppress ghost task notifications |

---

## 4. Technical Debt Audit and Prioritization Summary

- **Total items:** 15
- **Release blockers:** 0
- **TD-001 (Git Hygiene):** First implementation, partially resolved. Added `.gitignore` rules for `/reports/*.json` and lockfile policy comments. Remains only partially resolved because many untracked Markdown and dependency files are still not covered.
- **TD-003 (Missing OpenAPI paths):** Remained noted, not yet addressed.
- **Order-dependent test issue and other caveats:** May remain; flagged in the audit for future review.

Artifacts:
- `reports/aee_technical_debt_audit.md`
- `reports/aee_technical_debt_prioritization_review.md`

---

## 5. TD-001 Workflow Completed

| Stage | Run ID |
|---|---|
| Implementation | `run_ddc33613cee442e88edfa1a46419ad4a` |
| Review | `run_0e5fd028a2494f04bfbefab5f1fe0bbe` |
| Commit | `run_18d06b9f96134981a47f58ff32abf279` |
| Push | `run_f69a471817a8473c9207fbc733fbeba0` |

- `.gitignore` added `/reports/*.json` and lockfile policy comments.
- Commit and push successful — HEAD advanced to `ea18da9`.
- TD-001 remains only partially resolved due to many untracked Markdown/dependency files not yet covered by ignore rules.

Artifacts:
- `reports/aee_td_001_git_hygiene_implementation.md`
- `reports/aee_td_001_git_hygiene_review.md`
- `reports/aee_td_001_git_hygiene_commit.md`
- `reports/aee_td_001_git_hygiene_push.md`

---

## 6. Current Report Lifecycle Work

| Stage | Run ID | Artifact |
|---|---|---|
| Decision | `run_5e43c4b775e5474fabee9b9db241c3ec` | `reports/aee_report_lifecycle_decision.md` |
| Independent Review | `run_31cee26f5938499d93b22477b796689c` | `reports/aee_report_lifecycle_independent_review.md` |

**Decision summary:**
- Inventory at decision time: 172 untracked Markdown files.
- Initial dispositions: 168 ARCHIVE, 4 NEEDS_REVIEW, 0 KEEP_AND_TRACK, 0 TO_BE_DELETE.

**Independent review verdict: PASS WITH CAVEATS**
- 4 files originally marked ARCHIVE are directly referenced from 12 tracked source/test/doc locations.
- Therefore **Archive Batch Ready = NO**.
- Actual untracked Markdown count during review was 173 (the decision artifact itself had been added to the untracked set).
- No files moved, deleted, committed, or pushed during lifecycle review work.

---

## 7. Exact Next Step

Dispatch a **Minimal Finalization** work order (decision/planning only — no file moves, no deletions, no commits, no pushes).

The Minimal Finalization work order must:

1. Revise the classification for the 4 referenced Markdown files (originally marked ARCHIVE but directly referenced from tracked source/test/doc locations).
2. Correct the inaccurate claim about tracked references in the decision artifact.
3. Rebuild the exact Archive manifest with corrected dispositions.
4. Preserve P0-1 / shadow-run exclusions.
5. Produce a new verified durable artifact.

Then perform a **Minimal Re-review** of the finalized artifact.

Only if **Archive Batch Ready = YES** after re-review should an **Archive Move Batch** be proposed for user authorization.

Constraints:
- No actual deletion.
- Use archive or to-be-delete quarantine only.
- No commit or push without explicit user authorization.

---

## 8. Suggested First Prompt for the New Conversation

```
請接續 AEE 對話交接工作。先讀取 reports/aee_conversation_handoff_2026-07-31.md，然後用 Hermes Runtime 驗證以下現況：
1. git -C /home/ubuntu/hermes-runtime-bridge rev-parse HEAD 確認 HEAD 仍為 ea18da9afc611ac73f6e9ad97c603d291454214e。
2. 用 hermesGetRunSummary 確認 reports/aee_report_lifecycle_independent_review.md 的 review run（run_31cee26f5938499d93b22477b796689c）已完成且 verdict 為 PASS WITH CAVEATS。
3. 確認 8 個交接文件 SHA256 與 handoff 第 9 節一致。

驗證完成後，只派發一張 Minimal Finalization 工單（決策/規劃用，不移動、不刪除、不 commit、不 push）。工單內容：修正 4 個被引用 Markdown 的分類、更正 tracked references 不實陳述、重建精確 Archive manifest、保留 P0-1/shadow-run 排除項、產出新驗證持久化 artifact。完成後再做一次 Minimal Re-review。唯當 Archive Batch Ready = YES 才向使用者提案 Archive Move Batch。未經使用者明確授權不得 commit、push、刪除或移動任何檔案。
```

---

## 9. Important Artifact Paths and SHA256

| Artifact | SHA256 |
|---|---|
| `reports/aee_technical_debt_audit.md` | `0322afca7e5016e0c054fb202de73ac23d74f964dbe1b82c7db7713a189d7384` |
| `reports/aee_technical_debt_prioritization_review.md` | `7b3b34e4d7a42aa75bb7baf54d8e6e31b603dc7cce87b31d8edc6039b3773433` |
| `reports/aee_td_001_git_hygiene_implementation.md` | `1f5c54af4e4d49913912b63786c05d91cd1c3770b181ff0b63bfe91f117895dc` |
| `reports/aee_td_001_git_hygiene_review.md` | `ca5558eb6243d2b8a4313e4cec934f78507a565e682ef2748e0795194e846062` |
| `reports/aee_td_001_git_hygiene_commit.md` | `8539f726b3ed971a18cefa6d07d9e0a47cc58165988714354170ceb803970623` |
| `reports/aee_td_001_git_hygiene_push.md` | `451e912137c1f478d3d8ba7dab24fc77d5a973e90e01307121738334f89371ef` |
| `reports/aee_report_lifecycle_decision.md` | `cf8c41eb13518acb8fc0a4a58e4740f0b7761d51b393f9638db05999ec885d61` |
| `reports/aee_report_lifecycle_independent_review.md` | `3e0f01903e9eddc2b5bac362b6ff2cc9b14b2d5bd2c4c388e9f701a226d6b691` |

---

## 10. Safety Checklist

- [x] No repository source modified.
- [x] No git state modified (no commit, no push, no stage).
- [x] No existing artifacts modified.
- [x] Only the handoff Markdown file created.
- [x] P0-1 shadow run preserved.
- [x] All 8 referenced artifacts verified on disk with matching SHA256.

---

_End of handoff. Created 2026-07-31 by M2 (Hermes Orchestrator)._