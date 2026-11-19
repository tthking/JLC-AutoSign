#!/bin/bash
set -e

cd "$(dirname "$0")"

VENV_DIR=".venv"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "⏳ 首次运行正在初始化本地 Python 虚拟环境..."
  python3 -m venv "$VENV_DIR"
fi

if ! "$VENV_DIR/bin/python" -c "import requests, nacl, websocket" >/dev/null 2>&1; then
  echo "📦 正在安装所需依赖 (requests, pynacl, websocket-client)..."
  "$VENV_DIR/bin/pip" install --quiet requests pynacl websocket-client
fi

"$VENV_DIR/bin/python" update_jlc_token.py
EXIT_CODE=$?

echo ""
read -r -p "按回车键关闭窗口..." _
exit $EXIT_CODE
