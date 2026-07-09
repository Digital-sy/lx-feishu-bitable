#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/apps/lx-feishu-bitable}"
cd "$PROJECT_DIR"

if [ -d "venv" ]; then
  source venv/bin/activate
elif [ -d ".venv" ]; then
  source .venv/bin/activate
else
  echo "未找到 venv/.venv，请先执行部署步骤创建虚拟环境" >&2
  exit 1
fi

python -m jobs.sync_product_performance "$@"
