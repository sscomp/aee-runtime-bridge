# TASK-M12 — GPT Action Publication Readiness Review

> **Task Order Document (Durable Artifact)**
> Author: M2 (Hermes, Abacus.ai runtime)
> Created: 2026-07-15
> Target executor: Claude Executor / Hermes subagent (read-only review)
> Scope tag: `read-only-review`
> Safety class: NO production mutations. NO source edits, NO commit, NO push, NO deploy, NO restart, NO GPT Action publish, NO credential touch.

---

## 1. Goal

判定 ChatGPT Custom GPT Action 是否可以**直接以靜態 `openapi.yaml` 作為權威 schema** 完成最終發布，而不需要 live `/openapi.json` 在 `components.securitySchemes.bearerAuth` 上同步。

- 若 **A) 可以** → 推薦進入 `TASK-M13 Final Publication & End-to-End Validation`，本任務不做任何 source 變更。
- 若 **B) live `/openapi.json` 為 GPT Builder 必要輸入** → 推薦後續 `TASK-M12.1 Minimal bearerAuth OpenAPI fix`，本任務**僅產出建議**，**不實作**任何 code fix。

本任務只交付一份審查報告（本檔 + 最終 verdict 段落）。

---

## 2. Background / Current State

**M11 runtime restart + live smoke 已 PASS。** 已驗證項目（M11 結果，本任務不再重跑）:

1. Claude Executor 可用
2. Hermes legacy route 可達
3. Metadata routing 正確
4. Verified manifest gate 正常
5. Env loading 正常
6. Process cleanup 正常
7. Secret safety 通過
8. Runtime authentication（BRIDGE_API_KEY bearer）實際可用

**已知唯一未解缺口:**

- 靜態 `openapi.yaml` 已定義 `components.securitySchemes.bearerAuth`（http / bearer / opaque）— 第 42–46 行
- Live `/openapi.json` 回傳的 schema **缺少** `components.securitySchemes`（`has_securitySchemes: False`, `has_bearerAuth: False`），但 `paths` 55 個端點齊全、`info.title=Hermes Runtime Bridge`、version `1.3.0-aee2`
- Runtime auth 仍正常運作（bearer key 在 request header 驗證），即「schema 沒 advertise、auth 仍有效」

**M2 本次任務前 baseline 採集（2026-07-15）:**

| 檢查 | 結果 |
|---|---|
| `/home/ubuntu/hermes-runtime-bridge` branch | `master` |
| HEAD | `da9f5667473da5c2468c5bc675b89fcef29b9a7a` |
| `git status --short` tracked changes | 0 |
| Staged files | 0 |
| Untracked count | 18（皆為前期報告 `*.md` + `data/`，與本任務無關） |
| `git diff --shortstat` | 空（working tree clean on tracked files） |
| `openapi.yaml` 大小 | 21.2K |
| `openapi.yaml` 含 `securitySchemes` | ✅ 第 42 行 |
| `openapi.yaml` 含 `bearerAuth` | ✅ 第 43 行 |
| live `/openapi.json` 含 `securitySchemes` | ❌ |
| live `/openapi.json` paths 數 | 55 |
| live `/health` | `status:ok`, hermes reachable, dispatcher 12 tasks |

---

## 3. Scope

本任務**只做審查與建議**:

1. 比對靜態 `openapi.yaml` 與 live `/openapi.json` 在 `components.securitySchemes` 上的差異
2. 評估 ChatGPT GPT Builder 對 OpenAPI schema 來源的接受條件
3. 評估 `security` top-level field / per-operation security requirement 在兩份 schema 的呈現
4. 判定 GPT Builder 是否可用靜態 `openapi.yaml` 直接發布
5. 產出 PASS / PASS WITH CAVEATS / FAIL verdict 與後續任務對應

---

## 4. Explicit Non-Goals / Forbidden Actions

**禁止動作（任何一條觸發即視為任務 FAIL）:**

- ❌ 修改任何 source code（`app.py` / `dispatcher/*` / `openapi.yaml` / `openapi.json` generator）
- ❌ `git commit` / `git push` / `git add` / `git stash` 任何檔案
- ❌ 重啟 bridge、重啟 gateway、`supervisorctl restart`、kill 任何 process
- ❌ 在 ChatGPT GPT Builder 點 "Publish" / "Deploy" / "Create action"
- ❌ 觸碰 `~/.hermes/.env`、`BRIDGE_API_KEY`、Cloudflare token、任何 credentials
- ❌ 修改 `~/.hermes/cron/jobs.json` 或建立任何 cron job
- ❌ 寫任何 `.py` / `.yaml` / `.json` 到 `/home/ubuntu/hermes-runtime-bridge/`（**唯一例外**: 本任務交付檔 `TASK-M12_GPT_ACTION_PUBLICATION_READINESS_REVIEW.md`）
- ❌ `git add -A`
- ❌ 對外發布任何含 secret 的訊息

---

## 5. Inputs / Artifacts to Inspect

| 路徑 | 用途 |
|---|---|
| `/home/ubuntu/hermes-runtime-bridge/openapi.yaml` | 靜態權威 schema 候選 |
| `https://hermes-runtime.biaobecue.com/openapi.json` | live schema（curl + `User-Agent: curl/7.88.1`） |
| `https://hermes-runtime.biaobecue.com/health` | live 服務健康驗證 |
| `/home/ubuntu/hermes-runtime-bridge/app.py` | **唯讀** — 找 OpenAPI schema 生成邏輯（`get_openapi` / `app.openapi()` / 自訂 generator），確認為何 live schema 缺 `securitySchemes` |
| `/home/ubuntu/hermes-runtime-bridge/README*.md` | 既有 GPT Action 說明文件 |
| ChatGPT GPT Builder 文件（公開 web） | 確認 GPT Builder 對 schema 來源（file upload vs URL）的接受條件、是否要求 `securitySchemes` 必須出現在 submitted schema |

---

## 6. Required Review Steps

### Step 1 — Diff static vs live schema（結構比對）
- 用 `python3 -c` + `requests`/`urllib`（**User-Agent: curl/7.88.1**，避免 Cloudflare WAF 擋 Python-urllib）抓 live `/openapi.json`
- 比對以下欄位在兩份 schema 的存在與值:
  - `openapi` version string
  - `info.title` / `info.version`
  - `servers`
  - `components.securitySchemes`（重點）
  - `components.schemas.<key>` 列表
  - `paths.<key>` 列表
  - top-level `security` field
  - 各 operation 的 `security` 欄位
- 產出對照表（差異清單）

### Step 2 — App.py schema 生成邏輯審查（唯讀）
- `grep -n "openapi\|securitySchemes\|bearerAuth\|app.openapi\|get_openapi\|OpenAPIResponse" app.py`
- 找出 live schema 為何缺 `securitySchemes`（例如: FastAPI `app.openapi()` 預設不會從 `openapi.yaml` 讀、而是從 routes + `app.openapi_components` 動態生成；若沒在 FastAPI app 上註冊 `HTTPBearer` security scheme，live schema 就不會 advertise）
- **只記錄 root cause，不修**

### Step 3 — GPT Builder 接受條件調查（web）
- 查 OpenAI 官方文件: Custom GPT Action 是否接受「上傳 OpenAPI YAML / JSON file」作為 schema 來源
- 查 GPT Builder 是否要求 `securitySchemes` 必須出現在 submitted schema（vs 在 GPT Builder UI 的 Authentication tab 手動設定 Bearer + key）
- 確認 `openapi.yaml` 第 34–40 行註解所述「Authentication is configured in the GPT Action Authentication tab」是否符合 GPT Builder 實際流程

### Step 4 — 風險評估
- 若 GPT Builder 用靜態 `openapi.yaml`:
  - 風險 1: 靜態 schema 與 live schema 的 `paths`/`schemas` 是否 drift？若有 drift，GPT 可能呼叫不存在或形狀錯誤的端點
  - 風險 2: `securitySchemes` 在靜態 schema 但 live 不 advertise → GPT Builder 能否正確觸發 bearer auth header？
- 量化 drift: 比對 `paths` key 集合差異、`schemas` key 集合差異

### Step 5 — Decision Gate 評估
依 §7 的兩分支條件判定 A 或 B。

### Step 6 — 產出 verdict + follow-up mapping
依 §9 verdict 格式填寫，並依 §10 對應後續任務。

### Step 7 — Telegram 通知嘗試
- 用 `hermes send --to telegram:5132341473 --subject "TASK-M12 verdict" --file <this-md> --json`（或 `--file -` stdin）
- 記錄 `success` / `message_id` / `mirrored`
- 失敗不影響 verdict，但必須列為 caveat 並附嘗試證據

---

## 7. Decision Gate

### Branch A — Static `openapi.yaml` 可直接作為權威 schema

**全部成立才選 A:**

1. GPT Builder 接受上傳 OpenAPI YAML file 作為 schema 來源（官方文件佐證）
2. GPT Builder 的 Authentication tab 可獨立設定 Bearer + key，不要求 `securitySchemes` 必須在 submitted schema 出現
3. 靜態 `openapi.yaml` 的 `paths` + `schemas` 與 live `/openapi.json` **無 functional drift**（path key 集合一致或差異僅在版本欄位/description）
4. 靜態 `openapi.yaml` 的 `servers.url` = `https://hermes-runtime.biaobecue.com` 與 live 一致
5. M11 已驗證 runtime auth 實際可用

**A 成立 → verdict PASS，推薦 TASK-M13，本任務不建議任何 source change。**

### Branch B — Live `/openapi.json` 為 GPT Builder 必要輸入

**任一成立即選 B:**

1. GPT Builder 要求 schema 必須來自可達 URL（不接受 file upload），或
2. GPT Builder 要求 `securitySchemes` 必須出現在 submitted schema 才能啟用 Bearer auth，或
3. 靜態 `openapi.yaml` 與 live `/openapi.json` 有 **functional path/schema drift**（GPT 會呼叫到形狀錯誤的端點）

**B 成立 → verdict PASS WITH CAVEATS（審查本身完成）但 Source Change Required = YES，推薦 TASK-M12.1。本任務不實作 fix。**

---

## 8. Exact Acceptance Criteria

本任務 PASS 需**全部**成立:

1. §6 Step 1–7 全部執行並有證據
2. static vs live schema 對照表已產出（至少含 `securitySchemes` / `paths` key 集合 / `servers` / `info.version`）
3. app.py schema 生成 root cause 已記錄（一行說明 + 證據 line number）
4. GPT Builder 接受條件有官方文件 URL 佐證
5. Decision Gate 已選 A 或 B 並附理由
6. §9 三項 verdict 已填
7. §10 follow-up mapping 已對應
8. Telegram 通知已嘗試（成功附 `message_id`；失敗附嘗試指令 + 錯誤）
9. 全程未觸碰 §4 任何禁止動作（self-attest + `git status` 證據）

---

## 9. Required Final Verdicts

填寫以下三項（**只能選一個值**）:

```
Verdict (審查任務本身):              PASS | PASS WITH CAVEATS | FAIL
GPT Action Publication Ready:        YES | NO
Source Change Required:              YES | NO
Final Mile (M12 → M13 publish) Complete:  YES | NO
```

語義:
- `PASS` = 審查完成 + Branch A 成立 + Publication Ready YES + Source Change NO + Final Mile Complete NO（M13 才完成）
- `PASS WITH CAVEATS` = 審查完成 + Branch B 成立 + Publication Ready NO + Source Change YES + Final Mile Complete NO
- `FAIL` = 審查未完成或觸發禁止動作

---

## 10. Follow-up Mapping

| 本任務結果 | 後續任務 | 動作 |
|---|---|---|
| Branch A 成立 | **TASK-M13 — Final GPT Action Publication & End-to-End Validation** | 在 GPT Builder 用靜態 `openapi.yaml` 建立 Action、設 Authentication tab Bearer + key、發布、跑 createRun → getTaskProgress → getRun E2E |
| Branch B 成立 | **TASK-M12.1 — Minimal bearerAuth OpenAPI fix** | 在 `app.py` 註冊 FastAPI `HTTPBearer` security scheme 或覆寫 `app.openapi()` 注入 `components.securitySchemes.bearerAuth`，使 live `/openapi.json` advertise bearerAuth。**最小變更、不改端點邏輯、不改 auth 行為**。本任務**只產出 fix 建議**，不實作 |

---

## 11. Production Safety Declaration

執行者必須在報告末尾宣告:

```
Production Safety Declaration:
- [ ] No source code modified
- [ ] No git commit / push / add / stash
- [ ] No process restart / kill / supervisorctl
- [ ] No GPT Action publish / deploy
- [ ] No credentials touched
- [ ] No cron job created or modified
- [ ] Only allowed artifact written: TASK-M12_GPT_ACTION_PUBLICATION_READINESS_REVIEW.md
- [ ] git status post-task shows 0 tracked changes (untracked baseline preserved)
```

並附 `git status --short` post-task 截圖（文字）作為證據。

---

## 12. Mandatory Final Report Template

執行者必須在 `TASK-M12_GPT_ACTION_PUBLICATION_READINESS_REVIEW.md` 末尾 append 以下段落（或產出附屬 verdict 段）:

```markdown
## M12 Final Report

### A. Execution Summary
- Executor: <name>
- Start (UTC): <ts>
- End (UTC): <ts>
- Duration: <min>

### B. Schema Diff (static openapi.yaml vs live /openapi.json)
| 欄位 | static | live | drift |
|---|---|---|---|
| openapi version | | | |
| info.title | | | |
| info.version | | | |
| servers | | | |
| components.securitySchemes | | | |
| components.securitySchemes.bearerAuth | | | |
| paths key set | | | |
| top-level security | | | |

### C. App.py Schema Generation Root Cause
- 一行說明 + 證據 line number

### D. GPT Builder Acceptance Evidence
- 官方文件 URL + 引述重點

### E. Decision Gate
- Branch selected: A | B
- 理由:

### F. Verdicts
- Verdict: PASS | PASS WITH CAVEATS | FAIL
- GPT Action Publication Ready: YES | NO
- Source Change Required: YES | NO
- Final Mile Complete: YES | NO

### G. Follow-up Recommendation
- Next task: TASK-M13 | TASK-M12.1

### H. Telegram Notification Attempt
- 指令:
- success: true | false
- message_id: <id or N/A>
- mirrored: true | false | N/A
- 失敗原因（若有）:

### I. Production Safety Declaration
- 全部 8 項 checkbox + post-task `git status --short` 證據
```

---

## 13. Telegram Attempt Required

本任務**必須嘗試** Telegram 通知鼎鼎（chat_id `5132341473`），不論 verdict 結果:

- 指令: `hermes send --to telegram:5132341473 --subject "TASK-M12 GPT Action Publication Readiness Review — verdict" --file /home/ubuntu/hermes-runtime-bridge/TASK-M12_GPT_ACTION_PUBLICATION_READINESS_REVIEW.md --json`
- 記錄 `success` / `message_id` / `mirrored`
- **不**在訊息中揭示 `BRIDGE_API_KEY` / 任何 secret
- 失敗不影響 verdict，但必須列為 caveat 並附嘗試指令 + 錯誤訊息

---

## 14. One Task, One Deliverable

- **唯一交付檔**: `/home/ubuntu/hermes-runtime-bridge/TASK-M12_GPT_ACTION_PUBLICATION_READINESS_REVIEW.md`
- 本檔即為 task order；執行者在末尾 append `## M12 Final Report` 段即為最終交付
- 不產生其他 `.py` / `.yaml` / `.json` / `.sh` 檔案
- 不修改 `openapi.yaml` / `openapi.json` / `app.py` / 任何 source

---

## 15. Baseline Evidence (M2 pre-task, 2026-07-15)

```
Repo: /home/ubuntu/hermes-runtime-bridge
Branch: master
HEAD: da9f5667473da5c2468c5bc675b89fcef29b9a7a
git status --short (tracked changes): 0
Staged: 0
Untracked: 18 (pre-existing reports + data/, unrelated to this task)
git diff --shortstat: empty

openapi.yaml: 21.2K, line 42 has securitySchemes, line 43 has bearerAuth
live /openapi.json: has_securitySchemes=False, has_bearerAuth=False, paths=55
live /health: status=ok, hermes=reachable, dispatcher 12 tasks
```

---

_End of task order. 執行者請在 `## M12 Final Report` 段開始工作。_