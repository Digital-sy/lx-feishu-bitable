#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/apps/lx-feishu-bitable}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$PROJECT_DIR"

if [ ! -d "venv" ]; then
  "$PYTHON_BIN" -m venv venv
fi

source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f ".env" ]; then
  cp config.example.env .env
  echo "已生成 .env，请先编辑数据库和飞书配置：$PROJECT_DIR/.env"
else
  echo ".env 已存在，未覆盖"
fi

echo "部署完成。可先执行：bash scripts/run_sync_product_performance.sh --dry-run"
