# Hermes M2 強化計畫（Phase 1）— 設計文件

**版本**：v1.0
**日期**：2026-07-07
**狀態**：Approved → Implementing
**作者**：M2（鼎鼎指導）

---

## 一、現況架構分析

### 1.1 既有元件

| 元件 | 角色 | 端點 | 持久化 |
|---|---|---|---|
| ChatGPT Custom GPT Action | 任務派發者（Planner/Orchestrator） | OpenAI 內部 | 無（GPT session 結束即遺失） |
| Hermes Runtime Bridge (`127.0.0.1:8787`) | 安全 façade、4 端點 thin pass-through | `/health` `/runs` `/runs/{id}` `/runs/{id}/summary` `/runs/{id}/stop` | 無 |
| Hermes M2 API Server (`127.0.0.1:8642`) | 任務執行 runtime、SSE event stream | `/v1/runs` 等 | Hermes 內部 session 機制（生命週期有限） |
| supervisord (PID 7) | 服務守護 | – | – |
| Cloudflare Tunnel | 對外暴露 `hermes-runtime.biaobecue.com` | – | – |

### 1.2 痛點定位

**SOP 第二節**寫的「ChatGPT 顯示任務已建立（Run ID），但 Hermes M2 無法追蹤該任務」對應到目前的程式碼：

1. **bridge 是 stateless 的**：4 個端點都是即時去問 Hermes 8642，本身不存任何東西。
2. **OpenAI Run ID 沒有反向連結**：GPT 那邊的 run_id 跟 Hermes 8642 的 run_id 是兩件事，bridge 沒記錄對應關係。
3. **任務完成後查不到**：Hermes 8642 的 run 狀態有 TTL，過了就 404；bridge 自己也沒 cache。
4. **沒有 progress 中繼站**：任務跑 15 分鐘，GPT 只能盲猜「可能還在跑」。
5. **沒有 audit / log 持久化**：誰派了什麼任務、用什麼 prompt、什麼模型、什麼 input/output 全部沒有。

### 1.3 強化目標

把 bridge 從「無狀態 proxy」升級成「**有狀態 task 編排層**」：

- 派任務時同步寫入 SQLite（task 主表 + event 子表 + log file + report 草稿）
- 用 Hermes 自己的 `TASK-YYYYMMDD-NNNN` 當主鍵，不再依賴 OpenAI Run ID
- 提供 progress 中繼更新（5/10/25/40/60/80/95/100%）+ 步驟名稱
- 提供 task 結束後的結構化 result metadata
- 提供 CLI 查詢（`hermes task list/show/logs/rerun`）
- 不破壞既有功能：4 個舊端點行為維持，只新增 `/tasks/*` 端點

---

## 二、模組切分

```
~/hermes-runtime-bridge/
├── app.py                          # FastAPI app（thin 路由層 + dispatcher 調用）
├── openapi.yaml                    # 對外 schema（既有 + 新增 /tasks/*）
├── dispatcher/
│   ├── __init__.py
│   ├── db.py                       # SQLite 連線池 + schema 管理
│   ├── ids.py                      # TASK-YYYYMMDD-NNNN 生成器
│   ├── manager.py                  # TaskManager — 狀態機核心
│   ├── progress.py                 # 5/10/25/40/60/80/95/100% 進度助手
│   └── models.py                   # Pydantic 資料模型（與 DB 對應）
├── research/
│   ├── __init__.py
│   ├── scheduler.py                # 排程 / cron 偵測
│   ├── repo.py                     # Repository 掃描（檔案、依賴、graph）
│   ├── prompts.py                  # Prompt 載入（指向 prompts/）
│   └── report.py                   # 報告 9 章節框架
├── prompts/
│   ├── macro_v1.md
│   ├── company_v2.md
│   ├── industry_v1.md
│   └── review_v3.md
├── config/
│   ├── scheduler.json
│   ├── report.json
│   ├── research.json
│   └── model.json
├── reports/                        # 報告輸出（每 task 一份 task.json + report.md）
│   └── TASK-XXX/
│       ├── task.json               # result metadata（SOP §4）
│       └── report.md               # 9 章節統一結構（SOP §5）
├── logs/
│   └── TASK-XXX.log                # 每 task 獨立 log（SOP §9）
├── data/
│   └── dispatcher.db               # SQLite（WAL mode）
├── cli/
│   └── hermes_task.py              # hermes task list/show/logs/rerun（SOP §11）
├── tests/
│   ├── test_dispatcher.py
│   ├── test_safety.py              # 既有
│   ├── test_health.sh              # 既有
│   └── ...
├── supervisor/hermes-runtime-bridge.conf
└── README.md
```

---

## 三、Task Dispatcher 設計

### 3.1 SQLite Schema

#### 3.1.1 `tasks` 表（主表）

```sql
CREATE TABLE tasks (
  task_id          TEXT PRIMARY KEY,            -- 'TASK-20260707-0001'
  title            TEXT NOT NULL,
  type             TEXT NOT NULL,                -- research / coding / ops / review / normal
  priority         INTEGER NOT NULL DEFAULT 50, -- 0..100，越小越優先
  owner            TEXT NOT NULL DEFAULT 'm2',
  status           TEXT NOT NULL,                -- pending/queued/running/waiting/completed/failed/cancelled
  progress_pct     INTEGER NOT NULL DEFAULT 0,   -- 0/5/10/25/40/60/80/95/100
  progress_step    TEXT,                         -- 'Reading Scheduler' / 'Generating Report' ...
  created_at       TEXT NOT NULL,                -- ISO 8601 UTC
  started_at       TEXT,
  finished_at      TEXT,
  duration_sec     REAL,
  input_text       TEXT,                         -- 完整 input（限長 64KB 跟 bridge 對齊）
  hermes_run_id    TEXT,                         -- 對應 Hermes 8642 run_id（nullable 任務排隊中）
  openai_run_id    TEXT,                         -- GPT 端的 run_id（如果有給）
  session_id       TEXT,                         -- 對應 Hermes session_id
  mode             TEXT,                         -- normal/research/coding/ops
  result_path      TEXT,                         -- reports/TASK-XXX/
  error_message    TEXT,                         -- 失敗時的錯誤
  warning_count    INTEGER NOT NULL DEFAULT 0,
  retry_count      INTEGER NOT NULL DEFAULT 0,
  prompt_version   TEXT,                         -- e.g. 'macro_v1'
  model_name       TEXT,                         -- e.g. 'claude-sonnet-4-6'
  git_commit       TEXT,
  git_branch       TEXT
);

CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_created_at ON tasks(created_at DESC);
CREATE INDEX idx_tasks_type ON tasks(type);
```

#### 3.1.2 `task_events` 表（事件子表，append-only）

```sql
CREATE TABLE task_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  ts            TEXT NOT NULL,
  kind          TEXT NOT NULL,        -- 'created' / 'queued' / 'started' / 'progress' / 'log' / 'output' / 'completed' / 'failed' / 'cancelled' / 'warning'
  payload_json  TEXT,                 -- 結構化 payload
  FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE INDEX idx_events_task ON task_events(task_id, id);
```

#### 3.1.3 `task_outputs` 表（最終輸出，多模態支援）

```sql
CREATE TABLE task_outputs (
  task_id     TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  output_text TEXT,                 -- 主要 output（Hermes 8642 的 output 欄位）
  usage_json  TEXT,                 -- token usage 等
  raw_json    TEXT                  -- 完整 upstream payload（保證能還原）
);
```

### 3.2 Task ID 生成器

格式：`TASK-YYYYMMDD-NNNN`

- NNNN 為當日序號，4 位數補零
- 同日遞增；跨日重置
- 透過查詢 SQLite 當日最大序號 +1 來原子產生（用 SQLite transaction 保證唯一）

### 3.3 Status 狀態機

```
            ┌───► cancelled
            │
pending ──► queued ──► running ──► completed
            │            │  ▲       │
            │            │  └───────┘ (retry)
            │            ├──► failed
            │            └──► waiting (需要 user 確認)
            │
            └─► cancelled (queue 階段放棄)
```

合法的轉換：

| 從 | 到 | 觸發 |
|---|---|---|
| pending | queued | dispatcher 排入 queue |
| pending | cancelled | 使用者手動取消 |
| queued | running | dispatcher 派給 Hermes 8642 成功 |
| queued | cancelled | 使用者手動取消 |
| running | waiting | progress 標記需要 user 確認 |
| waiting | running | user 確認後繼續 |
| running | completed | Hermes 回傳 status=completed |
| running | failed | Hermes 回傳 status=failed 或 exception |
| running | cancelled | 使用者手動 stop |
| failed | queued | retry（遞增 retry_count） |

### 3.4 Progress 助手

合法進度值：`0, 5, 10, 25, 40, 60, 80, 95, 100`

`update_progress(task_id, pct, step)`：
- 校驗 pct 必須是合法值
- 校驗新 pct 必須 ≥ 舊 pct（不能倒退）
- 寫入 `tasks.progress_pct` 與 `tasks.progress_step`
- 同步寫入 `task_events` 一筆 `kind='progress'`
- 同步寫入 `logs/TASK-XXX.log` 一行

### 3.5 Task Manager 公開 API

```python
class TaskManager:
    def create(title, type, priority, owner, input_text, session_id, mode, prompt_version, model_name) -> Task
    def queue(task_id) -> Task
    def start(task_id, hermes_run_id) -> Task
    def progress(task_id, pct, step) -> Task
    def log(task_id, line) -> None
    def complete(task_id, output, usage, raw) -> Task
    def fail(task_id, error_message) -> Task
    def cancel(task_id) -> Task
    def retry(task_id) -> Task
    def get(task_id) -> Optional[Task]
    def list(status=None, type=None, limit=50) -> List[Task]
    def events(task_id, limit=500) -> List[TaskEvent]
```

### 3.6 對外 API（OpenAPI 擴充）

保留既有 4 端點，**新增**以下 6 端點：

| Method | Path | 用途 |
|---|---|---|
| POST | `/tasks` | 建立新 task（內部用 — bridge 內部呼叫；對外仍走 `/runs` 觸發） |
| GET | `/tasks` | 列出 task（支援 status/type filter） |
| GET | `/tasks/{task_id}` | 取得單一 task 完整狀態 |
| GET | `/tasks/{task_id}/progress` | 取得最新 progress（給 ChatGPT 輕量 poll 用） |
| GET | `/tasks/{task_id}/logs` | 取得 task log（可指定 tail lines） |
| GET | `/tasks/{task_id}/result` | 取得 task result metadata + report 連結 |
| POST | `/tasks/{task_id}/cancel` | 取消 task（呼叫 Hermes 8642 stop + 標記 cancelled） |
| POST | `/tasks/{task_id}/rerun` | 重新執行 task（建立新 task_id，繼承設定） |

`POST /runs` 流程改寫：

```
1. 接受舊 request
2. danger_check（既有）
3. 透過 TaskManager 建立 task（status=queued, task_id=新生成）
4. 呼叫 Hermes 8642 /v1/runs 拿到 hermes_run_id
5. update_start(task_id, hermes_run_id)
6. 啟動背景 task：每 2 秒 poll Hermes 8642 拿 status + output，update_progress
7. 完成 / 失敗 / 取消時記錄最終狀態
8. 回傳 CreateRunResponse，**多帶 task_id 欄位**
```

`GET /runs/{run_id}` 改為：用 hermes_run_id 反查 task，回傳 task 完整狀態（讓 ChatGPT 拿到 task_id 後可以兩邊對得起來）。

### 3.7 Progress 中繼更新策略

Hermes 8642 本身不吐 progress（只有 status + last_event + output）。我們用以下三種啟發式推估 progress：

1. **時間啟發式**：若 task 跑了 N 秒、上限 900 秒，pct = min(95, int(N / 900 * 100))
2. **last_event 啟發式**（Hermes 8642 偶爾有事件）：根據事件關鍵字對應到 25/40/60/80
3. **output 增長啟發式**：若 output 已經 > 1000 chars 通常 ≥ 60%

這些是 heuristic、不精準但有中繼感。如果 task 跑太快（< 5 秒）就直接跳 100。

### 3.8 Error Handling

- 每個 task 有 `retry_count`，預設上限 3 次
- retry 時複製原 task 設定產生新 task_id，舊 task 標記為 `failed` 並保留 audit
- warning 不中斷任務（用 `task_events` 記錄 `kind=warning`）
- 真正的 fatal error 才會 `fail()`

### 3.9 Report 框架

每個 task 結束後產出：

`reports/TASK-XXX/task.json` — 結果 metadata：
```json
{
  "task_id": "TASK-20260707-0001",
  "title": "...",
  "type": "research",
  "started_at": "...",
  "finished_at": "...",
  "duration_sec": 123.4,
  "git_commit": "...",
  "git_branch": "main",
  "prompt_version": "macro_v1",
  "model_name": "claude-sonnet-4-6",
  "input_excerpt": "...",
  "output_excerpt": "...",
  "report_path": "reports/TASK-XXX/report.md"
}
```

`reports/TASK-XXX/report.md` — 9 章節統一結構（如果 type=research 才有）：
1. Executive Summary
2. Current Architecture
3. Current Workflow
4. Findings
5. Technical Debt
6. Optimization
7. Priority
8. Roadmap
9. Appendix

### 3.10 Prompt Version 管理

`prompts/<name>_v<n>.md` 集中存。`prompts.py` 提供 `load(name, version=None)`，預設讀最新。

`task.prompt_version` 紀錄實際用的版本（例如 `macro_v1`）。

### 3.11 Configuration 管理

`config/*.json` 集中存。簡單 dict 載入即可，必要時支援環境變數覆寫。

`config/scheduler.json` — 重試上限、queue 大小、poll 間隔
`config/report.json` — 報告章節順序、是否啟用
`config/research.json` — research agent 掃描路徑、忽略規則
`config/model.json` — 預設模型、fallback 順序

### 3.12 Logging

`logs/TASK-XXX.log` 每行一筆：
```
2026-07-07T12:34:56Z [INFO] created title=...
2026-07-07T12:34:57Z [INFO] queued
2026-07-07T12:35:00Z [INFO] started hermes_run_id=...
2026-07-07T12:35:10Z [PROGRESS] 25 Scanning repository
2026-07-07T12:35:30Z [PROGRESS] 60 Generating report
2026-07-07T12:35:45Z [INFO] completed duration=45.2s
```

### 3.13 Repository 掃描能力

`research/repo.py` 暴露 `scan(root_path)` 函式，回傳：
- 檔案清單（依副檔名分類）
- 套件依賴（requirements.txt、pyproject.toml）
- 簡單 import graph（從 .py 抓 import）
- 入口點（FastAPI app、CLI main）

回傳 dict 結構化資料，由 `report.py` 渲染成 Architecture Report。

### 3.14 Observability CLI

`cli/hermes_task.py` 提供四個子命令：

```bash
hermes task list                    # 最新 50 筆
hermes task list --status running
hermes task list --type research
hermes task show TASK-XXX
hermes task logs TASK-XXX --tail 100
hermes task rerun TASK-XXX
```

CLI 直接讀 `data/dispatcher.db`，不需透過 HTTP。可掛 alias `hermes-task` 簡化。

---

## 四、Phase 2 / Phase 3 建議（佔位）

詳見 `Hermes_M2_Phase2_3_Roadmap.md`。
