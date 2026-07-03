#!/bin/bash

set -euo pipefail

# Canvas runner. Copy setting/canvas_config.template.json to this path,
# then fill course/assignment/input/output fields there.
CONFIG_PATH_DEFAULT="setting/canvas_config.json"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

CONFIG_PATH="$CONFIG_PATH_DEFAULT"
if [ "$#" -gt 0 ] && [[ "$1" == *.json ]]; then
  CONFIG_PATH="$1"
  shift
fi

COMMAND="${1:-help}"
if [ "$#" -gt 0 ]; then
  shift
fi

case "$COMMAND" in
  fetch)
    if [ ! -f "$CONFIG_PATH" ]; then
      echo "错误：找不到 Canvas 配置文件 $CONFIG_PATH"
      echo "先复制模板：cp setting/canvas_config.template.json setting/canvas_config.json"
      exit 1
    fi
    ./run.sh "$CONFIG_PATH" --canvas-fetch-only "$@"
    ;;
  grade)
    if [ ! -f "$CONFIG_PATH" ]; then
      echo "错误：找不到 Canvas 配置文件 $CONFIG_PATH"
      echo "先复制模板：cp setting/canvas_config.template.json setting/canvas_config.json"
      exit 1
    fi
    ./run.sh "$CONFIG_PATH" --canvas-skip-upload "$@"
    ;;
  preview)
    if [ ! -f "$CONFIG_PATH" ]; then
      echo "错误：找不到 Canvas 配置文件 $CONFIG_PATH"
      echo "先复制模板：cp setting/canvas_config.template.json setting/canvas_config.json"
      exit 1
    fi
    ./run.sh "$CONFIG_PATH" --canvas-dry-run-upload "$@"
    ;;
  upload)
    if [ ! -f "$CONFIG_PATH" ]; then
      echo "错误：找不到 Canvas 配置文件 $CONFIG_PATH"
      echo "先复制模板：cp setting/canvas_config.template.json setting/canvas_config.json"
      exit 1
    fi
    ./run.sh "$CONFIG_PATH" "$@"
    ;;
  help|-h|--help)
    cat <<'EOF'
Canvas runner usage:

  ./run_canvas.sh fetch      # only download submissions from Canvas
  ./run_canvas.sh grade      # grade and write outputs, but do not upload
  ./run_canvas.sh preview    # preview Canvas upload without writing grades
  ./run_canvas.sh upload     # run full Canvas flow and upload grades

Use another config:

  ./run_canvas.sh setting/my_canvas_config.json grade

Before running:

  cp setting/env.template.sh setting/api_env.sh
  cp setting/canvas_config.template.json setting/canvas_config.json

Fill setting/api_env.sh with AI_GRADER_* and CANVAS_API_TOKEN.
Fill setting/canvas_config.json with answer, canvas_course_id, canvas_assignment_id, and output_dir.
EOF
    ;;
  *)
    echo "错误：未知命令 $COMMAND"
    echo "可用命令：fetch, grade, preview, upload"
    exit 1
    ;;
esac
