#!/usr/bin/env bash
# 聪明钱复盘台 · A股  本地网页版（macOS / Linux）
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 python3，请先安装 Python 3.8+"
  exit 1
fi
exec python3 server.py --open
