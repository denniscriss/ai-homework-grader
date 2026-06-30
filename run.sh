#!/bin/bash

set -euo pipefail

# User-facing settings:
# - CONFIG_PATH_DEFAULT points to the run config. You can also pass a config path as the first argument.
# - ENV_PATH_DEFAULT points to private API/Canvas secrets.
# - Data files and output directory are configured in setting/run_config.json, not in this script.
CONFIG_PATH_DEFAULT="setting/run_config.json"
ENV_PATH_DEFAULT="setting/api_env.sh"
PYTHON_BIN="${PYTHON_BIN:-python}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
cd "$PROJECT_ROOT" || exit 1

CONFIG_PATH="$CONFIG_PATH_DEFAULT"
if [ "$#" -gt 0 ] && [[ "$1" != -* ]]; then
  CONFIG_PATH="$1"
  shift
fi

if [ ! -f "$CONFIG_PATH" ]; then
  echo "错误：找不到配置文件 $CONFIG_PATH"
  echo "可以先复制模板：cp setting/run_config.template.json setting/run_config.json"
  exit 1
fi

if [ -f "$ENV_PATH_DEFAULT" ]; then
  source "$ENV_PATH_DEFAULT"
fi

MODE="$("$PYTHON_BIN" - "$CONFIG_PATH" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(str(config.get("mode", "local")).strip().lower())
PY
)"

case "$MODE" in
  local)
    if [ -z "${AI_GRADER_API_KEY:-}" ]; then
      echo "错误：AI_GRADER_API_KEY 没有设置"
      exit 1
    fi
    "$PYTHON_BIN" src/grade_homework_skill_patch.py --config "$CONFIG_PATH" "$@"
    ;;
  canvas)
    "$PYTHON_BIN" src/canvas_integration.py --config "$CONFIG_PATH" "$@"
    ;;
  *)
    echo "错误：mode 只能是 local 或 canvas，当前是：$MODE"
    exit 1
    ;;
esac
