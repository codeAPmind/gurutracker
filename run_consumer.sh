#!/bin/bash
# 加载环境变量并运行 signal_consumer（09:35 ET）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi
cd "$SCRIPT_DIR"
exec /Users/openclaw/miniconda3/envs/US_AutoTrader/bin/python -m executor.signal_consumer "$@"
