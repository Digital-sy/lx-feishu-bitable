#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${LX_FEISHU_PROJECT_DIR:-/opt/apps/lx-feishu-bitable}"
PYTHON_BIN="${LX_FEISHU_PYTHON_BIN:-${PROJECT_DIR}/venv/bin/python}"
LOCK_FILE="${LX_FEISHU_SHOOTING_LOCK_FILE:-/tmp/lx_feishu_shooting_metrics_daily.lock}"

APP_TOKEN="${FEISHU_SHOOTING_APP_TOKEN:-ERyub7DVlaNhHMs4QPYcQd09ndc}"
TABLE_ID="${FEISHU_SHOOTING_TABLE_ID:-tblWhRPQxJkvhJ73}"
VIEW_ID="${FEISHU_SHOOTING_VIEW_ID:-vewEsifcRG}"
SOURCE_SCHEMA="${FEISHU_SHOOTING_SOURCE_SCHEMA:-dws_db}"
SOURCE_TABLE="${FEISHU_SHOOTING_SOURCE_TABLE:-dws_op_listing_traffic_daily}"
COUNTRY="${FEISHU_SHOOTING_COUNTRY:-US}"

export TZ="Asia/Shanghai"
export PYTHONUNBUFFERED=1

mkdir -p "${PROJECT_DIR}/logs"
cd "${PROJECT_DIR}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "[$(date '+%F %T')] ERROR: Python 不存在或不可执行: ${PYTHON_BIN}" >&2
  exit 1
fi

if [[ ! -f "${PROJECT_DIR}/jobs/run_shooting_metrics_daily.py" ]]; then
  echo "[$(date '+%F %T')] ERROR: 每日任务入口不存在" >&2
  exit 1
fi

if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
  echo "[$(date '+%F %T')] ERROR: 缺少 ${PROJECT_DIR}/.env" >&2
  exit 1
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] WARN: 上一次拍摄效果任务仍在运行，本次跳过"
  exit 0
fi

echo "[$(date '+%F %T')] 开始执行拍摄效果每日回写"

set +e
"${PYTHON_BIN}" jobs/run_shooting_metrics_daily.py \
  --app-token "${APP_TOKEN}" \
  --table-id "${TABLE_ID}" \
  --view-id "${VIEW_ID}" \
  --source-schema "${SOURCE_SCHEMA}" \
  --source-table "${SOURCE_TABLE}" \
  --country "${COUNTRY}" \
  --notification-required
status=$?
set -e

if [[ ${status} -eq 0 ]]; then
  echo "[$(date '+%F %T')] 拍摄效果每日回写完成"
else
  echo "[$(date '+%F %T')] ERROR: 拍摄效果每日回写失败，退出码=${status}" >&2
fi

exit "${status}"
