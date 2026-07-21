#!/usr/bin/env bash
set -Eeuo pipefail

CRON_FILE="/etc/cron.d/lx-feishu-shooting-metrics-daily"

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: 请使用 root 执行此卸载脚本" >&2
  exit 1
fi

if [[ -f "${CRON_FILE}" ]]; then
  rm -f "${CRON_FILE}"
  echo "已删除定时配置: ${CRON_FILE}"
else
  echo "定时配置不存在，无需删除: ${CRON_FILE}"
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl restart cron
else
  service cron restart
fi

echo "拍摄效果每日定时任务已停用"
