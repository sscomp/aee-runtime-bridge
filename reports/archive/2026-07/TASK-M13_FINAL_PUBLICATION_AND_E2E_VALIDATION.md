# TASK-M13 — Final GPT Action Publication & End-to-End Validation

> **Task Order Document (Durable Artifact)**
> Author: M2 (Hermes, Abacus.ai runtime)
> Created: 2026-07-15
> Target executor: 鼎鼎 (Scott Chang) 在 ChatGPT GPT Builder 手動操作 + M2 遠端驗證
> Scope tag: `publication-and-e2e-validation`
> Safety class: 本任務**涉及對外發布動作**（GPT Action publish）。所有 source code / infrastructure / credentials / git 動作維持禁止；唯一允許的外部動作是 GPT Builder UI 內的 Action 建立 + 發布 + E2E smoke test。

---

## 1. Goal

完成 ChatGPT Custom GPT Action 的**最終發布**，並以**端到端驗證**確認以下完整鏈路暢通:

```
GPT (ChatGPT Orchestrator)
  → Runtime Bridge (https://hermes-runtime.biaobecue.com)
    → Executor (Claude Executor / Hermes legacy route)
      → Verified Manifest Gate
        → Artifact (task output / delivery)
```

本任務是 M11 → M12 → M13 最後一哩。M11 驗證 runtime restart + live smoke，M12 審查確認靜態 `openapi.yaml` 可作為權威 schema，本任務執行實際發布並跑完整 E2E。

---

## 2. Preconditions (M11 and M12 Complete)

### M11 — Runtime Restart + Live Smoke（已完成）

| 驗證項目 | 狀態 |
|---|---|
| Claude Executor 可用 | ✅ PASS |
| Hermes legacy route 可達 | ✅ PASS |
| Metadata routing 正確 | ✅ PASS |
| Verified manifest gate 正常 | ✅ PASS |
| Env loading 正常 | ✅ PASS |
| Process cleanup 正常 | ✅ PASS |
| Secret safety 通過 | ✅ PASS |
| Runtime authentication（BRIDGE_API_KEY bearer）實際可用 | ✅ PASS |

### M12 — GPT Action Publication Readiness Review（已完成）

| 審查項目 | 結果 |
|---|---|
| Branch selected | **A** — 靜態 `openapi.yaml` 可直接作為權威 schema |
| Verdict | PASS |
| GPT Action Publication Ready | YES |
| Source Change Required | NO |
| Live `/openapi.json` 缺 `securitySchemes` | 已知缺口，不影響 GPT Builder 發布（auth 在 GPT Action Authentication tab 手動設定） |
| 靜態 `openapi.yaml` `paths` + `schemas` 與 live 無 functional drift | ✅ 確認 |
| `servers.url` = `https://hermes-runtime.biaobecue.com` | ✅ 確認（openapi.yaml 第 31 行） |

**M12 結論:** 直接進入 M13 最終發布，不需 TASK-M12.1 source fix。

### Baseline Evidence（M2 pre-task, 2026-07-15）

```
Repo: /home/ubuntu/hermes-runtime-bridge
Branch: master
HEAD: da9f5667473da5c2468c5bc675b89fcef29b9a7a
git status --short (tracked changes): 0
Staged: 0
Untracked: 18+ (pre-existing reports + data/, unrelated to this task)
git diff --shortstat: empty

openapi.yaml: 21.2K, line 42 securitySchemes, line 43 bearerAuth
live /openapi.json: has_securitySchemes=False, has_bearerAuth=False, paths=55
live /health: status=ok, hermes=reachable, dispatcher 12 tasks
```

---

## 3. Scope

本任務分兩階段:

### Phase A — GPT Action Publication（鼎鼎手動操作 in GPT Builder）

1. 在 ChatGPT GPT Builder 建立 / 更新 Custom GPT Action
2. 以靜態 `openapi.yaml` 作為 schema 來源（上傳 file 或貼入內容）
3. 在 Authentication tab 設定 Bearer + BRIDGE_API_KEY
4. 發布 Action
5. 確認 GPT Builder 顯示 Action available

### Phase B — End-to-End Validation（M2 遠端驗證 + 鼎鼎 GPT 端操作）

1. 從 ChatGPT GPT 發起 `createRun` → 取得 `task_id`
2. 輪詢 `getTaskProgress(task_id)` → 確認 task 進入 RUNNING → COMPLETED
3. 取得 `getRun` → 確認最終 output / artifact
4. M2 端確認 `dispatcher.db` 有對應 task row（status=completed, delivery_json 非 null）
5. M2 端確認 manifest gate 通過（executor 輸出符合 verified manifest）
6. M2 端確認 artifact 落地（檔案存在、非 0 byte）

---

## 4. Explicit Non-Goals / Forbidden Actions

**禁止動作（任何一條觸發即視為任務 FAIL）:**

- ❌ 修改任何 source code（`app.py` / `dispatcher/*` / `openapi.yaml` / `openapi.json` generator）
- ❌ `git commit` / `git push` / `git add` / `git stash` 任何檔案
- ❌ 重啟 bridge、重啟 gateway、`supervisorctl restart`、kill 任何 process
- ❌ 觸碰 `~/.hermes/.env`、`BRIDGE_API_KEY`、Cloudflare token、任何 credentials（**唯一例外**: 鼎鼎在 GPT Builder Authentication tab 貼入 BRIDGE_API_KEY，這是 GPT Builder UI 內動作，不經過 M2）
- ❌ 修改 `~/.hermes/cron/jobs.json` 或建立任何 cron job
- ❌ 寫任何 `.py` / `.yaml` / `.json` 到 `/home/ubuntu/hermes-runtime-bridge/`（**唯一例外**: 本任務交付檔 `TASK-M13_FINAL_PUBLICATION_AND_E2E_VALIDATION.md` + append 的 `## M13 Final Report` 段）
- ❌ `git add -A`
- ❌ 對外發布任何含 secret 的訊息（Telegram 通知不含 BRIDGE_API_KEY）
- ❌ 在 GPT Builder 以外的平台發布任何 API endpoint

**允許的動作:**

- ✅ 鼎鼎在 ChatGPT GPT Builder UI 建立 / 更新 / 發布 Custom GPT Action
- ✅ 鼎鼎在 GPT Builder Authentication tab 設定 Bearer + BRIDGE_API_KEY
- ✅ 鼎鼎在 ChatGPT 內呼叫 Action 端點發起 E2E smoke test
- ✅ M2 遠端查詢 `dispatcher.db` / `GET /health` / `GET /tasks/{id}/output`（唯讀）
- ✅ M2 寫入本任務交付檔 + append final report 段
- ✅ M2 嘗試 Telegram 通知鼎鼎

---

## 5. Publication Procedure

### Phase A — GPT Action Publication Steps（鼎鼎在 ChatGPT GPT Builder 操作）

#### Step A1 — 取得靜態 OpenAPI schema

M2 提供 `openapi.yaml` 內容給鼎鼎。來源: `/home/ubuntu/hermes-runtime-bridge/openapi.yaml`（21.2K，含 `securitySchemes.bearerAuth`）。

#### Step A2 — 在 GPT Builder 建立 / 更新 Action

1. 進入 ChatGPT → Settings → Beta → Custom GPTs → Create New GPT（或編輯現有 Hermes Orchestrator GPT）
2. 在 Actions 區段，選 "Create new action" 或編輯既有 action
3. Schema 來源選擇: **Upload file** 或 **Paste schema**，使用 `openapi.yaml` 全文
4. 確認 GPT Builder parse schema 成功（顯示 55 個端點，title = Hermes Runtime Bridge）

#### Step A3 — 設定 Authentication

在 Action 的 Authentication tab:

| 欄位 | 值 |
|---|---|
| Type | API Key |
| Auth Type | Bearer |
| API Key | `<BRIDGE_API_KEY>`（鼎鼎從安全來源取得，不經 M2 傳遞） |

#### Step A4 — 發布 Action

1. 確認 Action schema + auth 設定完成
2. Save → Publish（或 Update，若編輯既有 GPT）
3. 確認 GPT Builder 顯示 Action 為 available / published

#### Step A5 — 驗證 Action 可見

在 ChatGPT 對話中確認 GPT 可看到 Action（通常在 available tools / functions 列表顯示）。

---

## 6. GPT Action Import Procedure (Approved OpenAPI Source from M12)

### Approved Schema Source

M12 Decision Gate Branch A 核准的 schema 來源:

| 屬性 | 值 |
|---|---|
| 檔案路徑 | `/home/ubuntu/hermes-runtime-bridge/openapi.yaml` |
| 格式 | OpenAPI 3.1.0 YAML |
| 大小 | 21.2K |
| 端點數 | 55 |
| `components.securitySchemes.bearerAuth` | ✅ 第 42–46 行（type: http, scheme: bearer, bearerFormat: opaque） |
| `servers.url` | `https://hermes-runtime.biaobecue.com`（第 31 行） |
| `info.title` | Hermes Runtime Bridge |
| `info.version` | 1.2.0（靜態檔） / 1.3.0-aee2（live，差異僅在版本號，非 functional drift） |

### Import Steps

1. M2 將 `openapi.yaml` 內容提供給鼎鼎（可透過 `cat openapi.yaml` 在 terminal 顯示，或寫到暫存檔讓鼎鼎從 Abacus UI file explorer 下載）
2. 鼎鼎在 GPT Builder Actions tab 貼入 / 上傳 schema
3. 確認 GPT Builder 顯示 schema 解析成功
4. 若 GPT Builder 報 schema 錯誤: 記錄錯誤訊息，回報 M2，**不要**自行修改 `openapi.yaml`

### Authentication Configuration

GPT Builder Authentication tab 設定（不經 schema，獨立設定）:

```
Type:      API Key
Auth Type: Bearer
API Key:   <BRIDGE_API_KEY>  ← 鼎鼎從安全來源取得
Header:    Authorization: Bearer <key>  ← GPT Builder 自動組裝
```

### 重要提醒

- GPT Builder **不需要** live `/openapi.json` advertise `securitySchemes` — auth 在 UI tab 手動設定
- GPT Builder **不需要** schema 內 `security` top-level field — 預設所有端點都帶 auth header
- 靜態 `openapi.yaml` 的 `servers.url` 指向 Cloudflare Tunnel 公開端點，GPT 直接呼叫

---

## 7. End-to-End Validation Checklist

### Stage 1 — GPT → Runtime Bridge

| # | 檢查項目 | 預期結果 | 驗證方式 |
|---|---|---|---|
| 1.1 | GPT Action schema 已匯入 | 55 端點可見 | GPT Builder Actions 列表 |
| 1.2 | GPT Action auth 已設定 | Bearer + BRIDGE_API_KEY | Authentication tab 確認 |
| 1.3 | GPT 發起 createRun | HTTP 200 + `task_id` 回傳 | ChatGPT 對話觸發 |
| 1.4 | Bridge 收到 request | dispatcher.db 新增 task row | M2: `sqlite3 dispatcher.db "SELECT task_id,status FROM tasks ORDER BY created_at DESC LIMIT 1"` |

### Stage 2 — Runtime → Executor

| # | 檢查項目 | 預期結果 | 驗證方式 |
|---|---|---|---|
| 2.1 | Task 進入 RUNNING | status=running | M2: `getTaskProgress(task_id)` 或 DB query |
| 2.2 | Executor 啟動 | Claude Executor / Hermes route 被觸發 | dispatcher log 有 `executor_started` 事件 |
| 2.3 | Task 完成或失敗 | status=completed 或 status=failed | M2: DB query |

### Stage 3 — Executor → Manifest Gate

| # | 檢查項目 | 預期結果 | 驗證方式 |
|---|---|---|---|
| 3.1 | Manifest gate 收到 executor 輸出 | gate 被觸發 | executor_router log |
| 3.2 | Manifest gate 驗證通過 | gate result = pass | M2: `executor_router` log 或 DB |
| 3.3 | Manifest gate 拒絕時有明確原因 | rejection reason 記錄 | 若 status=failed，查 log |

### Stage 4 — Manifest → Artifact

| # | 檢查項目 | 預期結果 | 驗證方式 |
|---|---|---|---|
| 4.1 | Task output 落地 | `delivery_json` 非 null | M2: `GET /tasks/{id}/output` |
| 4.2 | Artifact 檔案存在 | `exists=true` in delivery_json | M2: delivery_json 解析 |
| 4.3 | Artifact 非 0 byte | `size > 0` | M2: `os.stat(path)` 或 delivery_json |
| 4.4 | GPT 收到最終結果 | ChatGPT 顯示 task output | 鼎鼎確認 ChatGPT 回應 |

### Stage 5 — Full Chain Confirmation

| # | 檢查項目 | 預期結果 | 驗證方式 |
|---|---|---|---|
| 5.1 | 整條鏈路無斷裂 | Stage 1–4 全 PASS | 本表全部 ✅ |
| 5.2 | 無 unintended side effect | git status 0 tracked changes, no process anomaly | M2: `git status --short` post-task |
| 5.3 | Telegram 通知已送達 | success=true, message_id 非 null | M2: `hermes send --json` |

---

## 8. Rollback Criteria

若 E2E 驗證 FAIL，依以下順序 rollback:

### Level 1 — GPT Action 層

| 觸發條件 | Rollback 動作 | 執行者 |
|---|---|---|
| GPT Builder schema 解析失敗 | 不發布，保留既有 action 狀態 | 鼎鼎 |
| createRun 回傳非 200 | 確認 auth header 正確，不 retry 超過 3 次 | 鼎鼎 |
| createRun 回 401/403 | 確認 BRIDGE_API_KEY 正確，不暴露 key | 鼎鼎 |

### Level 2 — Runtime Bridge 層

| 觸發條件 | Rollback 動作 | 執行者 |
|---|---|---|
| Bridge 回 5xx | M2 查 `/health` + dispatcher log，不重啟 bridge | M2 |
| Task 卡在 RUNNING 超過 5 分鐘 | M2 查 task log，確認 executor 是否啟動 | M2 |
| Task status=failed + manifest gate rejection | M2 查 executor_router log 確認 rejection reason | M2 |

### Level 3 — Executor 層

| 觸發條件 | Rollback 動作 | 執行者 |
|---|---|---|
| Claude Executor 無回應 | M2 確認 executor process 狀態，不 kill | M2 |
| Hermes legacy route 不可達 | M2 確認 Hermes gateway 狀態（不重啟） | M2 |

### Rollback 紅線

- ❌ **不允許** 為了 rollback 修改 source code
- ❌ **不允許** 為了 rollback `git commit` / `git push`
- ❌ **不允許** 為了 rollback 重啟任何 process
- ✅ **允許** 鼎鼎在 GPT Builder **停用 / 刪除 Action**（GPT Builder UI 內操作，不影響 runtime）
- ✅ **允許** M2 唯讀查詢所有 log / DB / health endpoint

---

## 9. Acceptance Criteria

本任務 PASS 需**全部**成立:

1. §5 Phase A Step A1–A5 全部完成（GPT Action 已發布，Action available）
2. §7 Stage 1–5 每一項檢查都有結果（PASS 或附 failure 證據）
3. 至少一個完整的 `createRun → getTaskProgress → getRun` 週期跑完
4. Task 最終 status = completed（或附 failure 證據 + root cause）
5. `delivery_json` 非 null 且 `exists=true`（或附 artifact 缺失證據）
6. 全程未觸碰 §4 任何禁止動作（self-attest + `git status` 證據）
7. §11 Production Safety Declaration 全部 checkbox 打勾
8. §12 Final Report 段落已填寫完整
9. §13 Telegram 通知已嘗試（成功附 `message_id`；失敗附嘗試指令 + 錯誤）
10. `git status --short` post-task 顯示 0 tracked changes（untracked baseline preserved）

---

## 10. Required Evidence

執行者必須在 `## M13 Final Report` 段附以下證據:

| # | 證據 | 取得方式 |
|---|---|---|
| E1 | GPT Action 發布確認截圖 / 描述 | 鼎鼎在 GPT Builder 確認 |
| E2 | createRun response（含 task_id） | ChatGPT 對話或 `curl` 輸出 |
| E3 | getTaskProgress 最終 status | M2: DB query 或 API call |
| E4 | getRun / getTask output | M2: `GET /tasks/{id}/output` |
| E5 | dispatcher.db task row | M2: `sqlite3 dispatcher.db "SELECT ..." ` |
| E6 | delivery_json 內容 | M2: API response 或 DB query |
| E7 | artifact 檔案 stat | M2: `ls -la <path>` 或 `os.stat` |
| E8 | git status post-task | M2: `git status --short` |
| E9 | Telegram notification result | M2: `hermes send --json` output |
| E10 | `/health` post-task | M2: `curl https://hermes-runtime.biaobecue.com/health` |

---

## 11. Final Decision Matrix

| E2E 結果 | Verdict | 後續動作 |
|---|---|---|
| Stage 1–5 全 PASS + artifact 存在 | **PASS** — M13 complete, GPT Action 正式上線 | 記錄上線狀態，關閉 M-series |
| Stage 1–3 PASS, Stage 4 artifact 缺失 | **PASS WITH CAVEATS** — pipeline 通但 delivery verification 需追查 | 開 TASK-M14: delivery verification deep dive |
| Stage 1–2 PASS, Stage 3 manifest gate reject | **PASS WITH CAVEATS** — executor 可達但 manifest gate 有問題 | 開 TASK-M14: manifest gate fix |
| Stage 1 PASS, Stage 2 executor 無回應 | **FAIL** — executor layer 問題 | 開 TASK-M11.1: executor restart（需鼎鼎 approve） |
| Stage 1 createRun 失敗（401/403） | **FAIL** — auth 設定問題 | 鼎鼎確認 BRIDGE_API_KEY + GPT Builder auth tab，重試 |
| Stage 1 createRun 失敗（5xx） | **FAIL** — runtime bridge 問題 | M2 查 `/health` + bridge log，開 TASK-M11.2 |
| 觸發任何 §4 禁止動作 | **FAIL** — safety violation | 立即停止，記錄違規，通知鼎鼎 |

---

## 12. Production Safety Declaration

執行者必須在報告末尾宣告:

```
Production Safety Declaration:
- [ ] No source code modified
- [ ] No git commit / push / add / stash
- [ ] No process restart / kill / supervisorctl
- [ ] No credentials touched (except 鼎鼎 in GPT Builder auth tab)
- [ ] No cron job created or modified
- [ ] Only allowed artifact written: TASK-M13_FINAL_PUBLICATION_AND_E2E_VALIDATION.md
- [ ] git status post-task shows 0 tracked changes (untracked baseline preserved)
- [ ] No secrets leaked in Telegram notification
```

並附 `git status --short` post-task 輸出作為證據。

---

## 13. Telegram Requirement

本任務**必須嘗試** Telegram 通知鼎鼎（chat_id `5132341473`）:

```
hermes send --to telegram:5132341473 \
  --subject "TASK-M13 Final Publication & E2E Validation — verdict" \
  --file /home/ubuntu/hermes-runtime-bridge/TASK-M13_FINAL_PUBLICATION_AND_E2E_VALIDATION.md \
  --json
```

- 記錄 `success` / `message_id` / `mirrored`
- **不**在訊息中揭示 `BRIDGE_API_KEY` / 任何 secret
- 失敗不影響 verdict，但必須列為 caveat 並附嘗試指令 + 錯誤訊息

### Telegram 簡版格式（依鼎鼎 2026-07-13 確認偏好）

Telegram 通知應同時送簡版摘要（不超過 15 行）:

```
✅ TASK-M13 Final Publication & E2E Validation
訊息類型: A-I 9-section final report
開始 (CST): <ts>
結束 (CST): <ts>
耗時: <min>
單號: TASK-M13
commit SHA: N/A (read-only task)
test count: E2E Stage 1-5 results
Verdict: PASS | PASS WITH CAVEATS | FAIL
摘要: <1-3 句>
完整報告: /home/ubuntu/hermes-runtime-bridge/TASK-M13_FINAL_PUBLICATION_AND_E2E_VALIDATION.md
```

---

## 14. Mandatory Final Report Template

執行者必須在 `TASK-M13_FINAL_PUBLICATION_AND_E2E_VALIDATION.md` 末尾 append 以下段落:

```markdown
## M13 Final Report

### A. Execution Summary
- Executor: <name>
- Start (UTC): <ts>
- End (UTC): <ts>
- Duration: <min>

### B. GPT Action Publication Result
- Action name: <name>
- Schema source: openapi.yaml (static, M12 Branch A approved)
- Schema parse: SUCCESS | FAIL
- Auth configured: YES | NO
- Action published: YES | NO
- GPT Action available in ChatGPT: YES | NO

### C. E2E Validation Results
| Stage | Check | Result | Evidence |
|---|---|---|---|
| 1.1 | GPT Action schema imported | | |
| 1.2 | GPT Action auth configured | | |
| 1.3 | createRun returns 200 + task_id | | |
| 1.4 | Bridge receives request | | |
| 2.1 | Task enters RUNNING | | |
| 2.2 | Executor starts | | |
| 2.3 | Task completes or fails | | |
| 3.1 | Manifest gate triggered | | |
| 3.2 | Manifest gate verdict | | |
| 3.3 | Rejection reason (if any) | | |
| 4.1 | delivery_json non-null | | |
| 4.2 | Artifact file exists | | |
| 4.3 | Artifact non-0-byte | | |
| 4.4 | GPT receives final result | | |
| 5.1 | Full chain no break | | |
| 5.2 | No unintended side effect | | |
| 5.3 | Telegram notification sent | | |

### D. Evidence Collected
- E1 GPT Action publish confirmation: <description/screenshot ref>
- E2 createRun response: <task_id>
- E3 getTaskProgress final status: <status>
- E4 getRun/getTask output: <summary>
- E5 dispatcher.db task row: <row data>
- E6 delivery_json: <summary>
- E7 artifact stat: <path, size, mtime>
- E8 git status post-task: <output>
- E9 Telegram result: <message_id, success, mirrored>
- E10 /health post-task: <status>

### E. Verdicts
- Verdict: PASS | PASS WITH CAVEATS | FAIL
- GPT Action Published: YES | NO
- E2E Chain Validated: YES | NO
- Final Mile Complete: YES | NO

### F. Decision Matrix Result
- Selected row: <row description>
- Follow-up action: <none / TASK-M14 description>

### G. Telegram Notification Attempt
- 指令: <command>
- success: true | false
- message_id: <id or N/A>
- mirrored: true | false | N/A
- 失敗原因（若有）: <reason>

### H. Production Safety Declaration
- 全部 8 項 checkbox + post-task `git status --short` 證據

### I. Caveats / Notes
- 任何 caveat、已知限制、後續建議
```

---

## 15. Baseline Evidence (M2 pre-task, 2026-07-15)

```
Repo: /home/ubuntu/hermes-runtime-bridge
Branch: master
HEAD: da9f5667473da5c2468c5bc675b89fcef29b9a7a
git status --short (tracked changes): 0
Staged: 0
Untracked: 18+ (pre-existing reports + data/, unrelated to this task)
git diff --shortstat: empty

openapi.yaml: 21.2K, line 42 securitySchemes, line 43 bearerAuth
live /openapi.json: has_securitySchemes=False, has_bearerAuth=False, paths=55
live /health: status=ok, hermes=reachable, dispatcher 12 tasks

M11 status: PASS (all 8 checks)
M12 status: PASS (Branch A, Publication Ready YES, Source Change NO)
```

---

_End of task order. 執行者請在 `## M13 Final Report` 段開始工作。_