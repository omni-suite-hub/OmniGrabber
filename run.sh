#!/usr/bin/env bash
# ============================================================
# OmniVideo / OmniGrabber 本地服务启动脚本 (macOS / Linux)
# ============================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 优先查找 python3 或 python
if command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PY_BIN="python"
else
    echo "错误: 未找到 Python 解释器。请先安装 Python 3.9+。"
    exit 1
fi

echo "正在调用跨平台启动器 ($PY_BIN start_server.py)..."
exec "$PY_BIN" "$DIR/start_server.py"
