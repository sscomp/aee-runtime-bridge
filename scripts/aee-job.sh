#!/bin/bash
# Read-only job lookup through an already configured local aee-bridge.
set -uo pipefail

usage() {
  cat <<'USAGE'
AEE 工單速查

用法：
  aee-job JOB_ID                        查目前狀態
  aee-job status JOB_ID                 查目前狀態
  aee-job result JOB_ID                 查結果、摘要與失敗原因
  aee-job watch JOB_ID [--interval 5]    定期查詢，結束後顯示結果
  aee-job status JOB_ID --json          輸出 JSON
  aee-job watch JOB_ID --json           每次查詢輸出一行 JSON（NDJSON）
  aee-job --help

支援 A3JOB、N2JOB、M2JOB 及 [A-Z][A-Z0-9]{0,15}JOB 前綴；後接 -YYYYMMDD-<32位小寫十六進位亂數>。
--interval：1–3600 秒整數，只用於 watch；預設 5 秒。
按 Ctrl+C 停止追蹤；已派出的任務會繼續執行。
使用本機 aee-bridge 驗證設定，不需輸入金鑰。
USAGE
}

die() { local code="$1"; shift; printf '錯誤：%s\n' "$*" >&2; exit "$code"; }

action=status
job_id=
json_output=0
interval=5
interval_set=0
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  status|result|watch) action="$1"; shift ;;
esac
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --json) json_output=1; shift ;;
    --interval)
      [ "$#" -ge 2 ] || die 2 '--interval 後須指定秒數。'
      interval="$2"; interval_set=1; shift 2 ;;
    -*) die 2 "不支援的選項：$1（請用 --help）。" ;;
    *)
      [ -z "$job_id" ] || die 2 '一次只查一筆工單。'
      job_id="$1"; shift ;;
  esac
done
[ -n "$job_id" ] || { usage >&2; exit 2; }
# Match aee/mcp_runtime/job_identity.py; the date is eight digits, not calendar-validated.
valid_job_id() {
  local LC_ALL=C
  local id_pattern='^[A-Z][A-Z0-9]{0,15}JOB-[0-9]{8}-[0-9a-f]{32}$'
  [[ "$1" =~ $id_pattern ]]
}
valid_job_id "$job_id" || die 2 '單號格式不正確，請貼上完整 JOB 單號（例如 M2JOB-YYYYMMDD-<32位小寫十六進位亂數>）。'
if [ "$interval_set" -eq 1 ] && [ "$action" != watch ]; then
  die 2 '--interval 僅用於 watch。'
fi
interval_pattern='^[1-9][0-9]{0,3}$'
[[ "$interval" =~ $interval_pattern ]] || die 2 '--interval 須為 1–3600 秒整數。'
[ "$interval" -le 3600 ] || die 2 '--interval 須為 1–3600 秒整數。'
command -v jq >/dev/null 2>&1 || die 4 '找不到 jq，請先安裝 jq。'
bridge="${AEE_BRIDGE_BIN:-${HOME}/.local/bin/aee-bridge}"
[ -x "$bridge" ] || die 4 "找不到可執行的 aee-bridge：$bridge"

# The helper privately reads the gateway credential. Never source its env
# file in a shell, evaluate input, or put credentials in process arguments.
fetch() {
  local command="$1"
  if ! response=$("$bridge" "$command" "$job_id" 2>/dev/null); then
    die 4 '無法查詢 Gateway。請確認 Gateway 已啟動及 aee-bridge 的環境正常。'
  fi
  printf '%s\n' "$response" | jq -e \
    'type == "object" and (.ok | type == "boolean")' >/dev/null 2>&1 \
    || die 4 'Gateway 回應不是預期的 JSON。'
  error_code=$(printf '%s\n' "$response" | jq -r '.error_code // ""')
  if [ "$error_code" = JOB_NOT_FOUND ]; then
    die 3 "Gateway 找不到此單號：${job_id}；請確認完整單號及派工主機。"
  fi
  # Validate identity and state without accepting arbitrary server text.
  state=$(printf '%s\n' "$response" | jq -r '.status // ""')
  response_id=$(printf '%s\n' "$response" | jq -r '.job_id // ""')
  [ "$response_id" = "$job_id" ] || die 4 "Gateway 查詢失敗：${error_code:-INVALID_RESPONSE}"
  case "$state" in
    queued|running|completed|failed|timed_out|cancelled) ;;
    *) die 4 "Gateway 查詢失敗：${error_code:-INVALID_RESPONSE}" ;;
  esac
  ok=$(printf '%s\n' "$response" | jq -r '.ok')
  if [ "$command" = job-status ] && [ "$ok" != true ]; then
    die 4 "狀態查詢失敗：${error_code:-INVALID_RESPONSE}"
  fi
  if [ "$command" = job-result ] && [ "$ok" != true ]; then
    case "$state:$error_code" in
      queued:JOB_NOT_COMPLETE|running:JOB_NOT_COMPLETE|failed:*|timed_out:*|cancelled:*) ;;
      *) die 4 "結果驗證失敗：${error_code:-INVALID_RESPONSE}" ;;
    esac
  fi
}

render() {
  local kind="$1"
  if [ "$json_output" -eq 1 ]; then
    printf '%s\n' "$response" | jq -c --arg kind "$kind" '{kind: $kind, data: .}'
    return
  fi
  printf '%s\n' "$response" | jq -r '
    def clean: tostring | gsub("[\u0000-\u0008\u000b-\u001f\u007f]"; "");
    def status_name: {queued:"排隊中", running:"執行中", completed:"已完成",
                failed:"失敗", timed_out:"逾時", cancelled:"已取消"}[.] // .;
    "單號：\(.job_id)",
    "狀態：\(.status)（\(.status | status_name)）",
    (if .agent then "Agent：\(.agent | clean)" else empty end),
    (if .created_at then "建立：\(.created_at | clean)" else empty end),
    (if .started_at then "開始：\(.started_at | clean)" else empty end),
    (if .finished_at then "結束：\(.finished_at | clean)" else empty end),
    (if .exit_code != null then "退出碼：\(.exit_code)" else empty end),
    (if .error_code then "錯誤碼：\(.error_code | clean)" else empty end),
    (if .error then "說明：\(.error | clean)" else empty end),
    (if .summary then "摘要：\n\(.summary | clean)" else empty end),
    (if .truncated == true then "摘要已截短；完整串流保留於主機。" else empty end)
  '
}

if [ "$action" = result ]; then
  fetch job-result
  render result || die 4 '結果格式化失敗。'
  exit 0
fi
trap 'printf "\n已停止追蹤；工單會繼續執行。\n" >&2; exit 130' INT
while :; do
  fetch job-status
  render status || die 4 '狀態格式化失敗。'
  [ "$action" = watch ] || exit 0
  case "$state" in
    queued|running)
      [ "$json_output" -eq 1 ] || printf '\n'
      sleep "$interval" ;;
    *)
      fetch job-result
      [ "$json_output" -eq 1 ] || printf '\n'
      render result || die 4 '結果格式化失敗。'
      exit 0 ;;
  esac
done
