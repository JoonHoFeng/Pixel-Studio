#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -m pip check >/dev/null 2>&1 || [ ! -f .venv/.pixel-installed ] || ! cmp -s requirements.txt .venv/.pixel-installed; then
  .venv/bin/python -m pip install -r requirements.txt
  cp requirements.txt .venv/.pixel-installed
fi
printf '\n像素工坊已启动，请打开 http://127.0.0.1:8765\n按 Ctrl+C 停止服务。\n\n'
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
