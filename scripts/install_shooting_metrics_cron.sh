#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${LX_FEISHU_PROJECT_DIR:-/opt/apps/lx-feishu-bitable}"
RUN_USER="${LX_FEISHU_CRON_USER:-$(id -un)}"
CRON_FILE="/etc/cron.d/lx-feishu-shooting-metrics-daily"
RUNNER="${PROJECT_DIR}/scripts/run_shooting_metrics_daily.sh"
LOG_FILE="${PROJECT_DIR}/logs/shooting_metrics_daily_cron.log"

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: 请使用 root 执行此安装脚本" >&2
  exit 1
fi

if ! id "${RUN_USER}" >/dev/null 2>&1; then
  echo "ERROR: 运行用户不存在: ${RUN_USER}" >&2
  exit 1
fi

if [[ ! -f "${RUNNER}" ]]; then
  echo "ERROR: 未找到运行脚本: ${RUNNER}" >&2
  exit 1
fi

if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
  echo "ERROR: 未找到 ${PROJECT_DIR}/.env，请先配置数据库和飞书参数" >&2
  exit 1
fi

if ! grep -q '^FEISHU_SHOOTING_NOTIFY_RECEIVE_ID=' "${PROJECT_DIR}/.env"; then
  echo "ERROR: .env 未配置 FEISHU_SHOOTING_NOTIFY_RECEIVE_ID" >&2
  exit 1
fi

mkdir -p "${PROJECT_DIR}/logs"
chmod +x "${RUNNER}"

cat > "${CRON_FILE}" <<EOF
# lx-feishu-bitable 拍摄效果跟踪每日任务
# 每日北京时间 15:00 执行；CRON_TZ 控制定时解释时区，TZ 控制任务日志时间。
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
CRON_TZ=Asia/Shanghai
TZ=Asia/Shanghai

0 15 * * * ${RUN_USER} ${RUNNER} >> ${LOG_FILE} 2>&1
EOF

chmod 0644 "${CRON_FILE}"

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable cron >/dev/null 2>&1 || true
  systemctl restart cron
else
  service cron restart
fi

echo "定时任务安装完成"
echo "执行时间: 每日北京时间 15:00"
echo "运行用户: ${RUN_USER}"
echo "配置文件: ${CRON_FILE}"
echo "运行脚本: ${RUNNER}"
echo "日志文件: ${LOG_FILE}"
echo
echo "查看配置: cat ${CRON_FILE}"
echo "查看日志: tail -f ${LOG_FILE}"
echo "立即测试: ${RUNNER}"
