#!/usr/bin/env python3
"""从 Canvas LMS 抓取指定作业的学生成绩，导出为三列 Excel 表格。

用法:
  # 抓取所有学生
  python src/fetch_grades.py --config setting/run_config.json --mode all

  # 仅抓取有成绩的学生
  python src/fetch_grades.py --config setting/run_config.json --mode graded

  # 仅抓取已提交作业的学生
  python src/fetch_grades.py --config setting/run_config.json --mode submitted

输出:
  canvas_grades_{作业名}.xlsx — 三列: 学号、姓名、成绩
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from canvas_integration import CanvasClient
from grade_homework_skill_patch import write_xlsx


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------

def fetch_grades(
    client: CanvasClient,
    course_id: str,
    assignment_id: str,
    mode: str,
) -> tuple[list[list[Any]], str]:
    """从 Canvas 抓取成绩并按模式过滤。

    返回:
        (rows, assignment_name)
        rows: 第一行为表头 ["学号", "姓名", "成绩"]，后续为数据行
        assignment_name: 作业名称，用于生成文件名
    """
    # 获取作业名称
    assignment = client.get_assignment(course_id, assignment_id)
    assignment_name = assignment.get("name", "未知作业")
    points_possible = assignment.get("points_possible")
    print(f"作业: {assignment_name} (满分: {points_possible or '未知'})")

    # 获取学生花名册
    roster = client.get_course_roster(course_id)

    # 获取提交记录
    submissions = client.get_submissions(course_id, assignment_id)

    # 建立 canvas_user_id → submission 映射
    sub_map: dict[str, dict[str, Any]] = {}
    for sub in submissions:
        uid = sub.get("canvas_user_id")
        if uid is not None:
            sub_map[str(uid)] = sub

    # 构建数据行
    rows: list[list[Any]] = [["学号", "姓名", "成绩"]]

    # 记录已在 roster 中处理过的 canvas_user_id
    matched_ids: set[str] = set()

    for entry in roster:
        uid = str(entry.get("canvas_user_id", ""))
        sid = str(entry.get("sis_user_id", ""))
        name = str(entry.get("name", ""))
        matched_ids.add(uid)

        sub = sub_map.get(uid)
        score = None
        has_submitted = False

        if sub:
            has_submitted = sub.get("workflow_state") not in ("unsubmitted",)
            if sub.get("score") is not None:
                score = sub["score"]

        # 根据模式过滤
        if mode == "graded" and score is None:
            continue
        if mode == "submitted" and not has_submitted:
            continue

        # 成绩为空时留空（None → ""）
        rows.append([sid, name, score if score is not None else ""])

    # 处理不在花名册中的提交（如旁听生）
    for sub in submissions:
        uid = str(sub.get("canvas_user_id", ""))
        if uid in matched_ids:
            continue
        if sub.get("workflow_state") == "unsubmitted":
            continue

        name = str(sub.get("user_name", ""))
        score = sub.get("score")

        if mode == "graded" and score is None:
            continue
        if mode == "submitted" and not (sub.get("workflow_state") not in ("unsubmitted",)):
            continue

        rows.append(["", name, score if score is not None else ""])

    # 按学号排序（跳过表头）
    header = rows[0]
    data = rows[1:]
    data.sort(key=lambda r: (str(r[0]), str(r[1])))
    rows = [header] + data

    return rows, assignment_name


def safe_filename(name: str) -> str:
    """将字符串转换为安全的文件名（去除特殊字符）。"""
    # 保留中文、字母、数字、空格、连字符
    cleaned = re.sub(r'[^\w\s一-鿿\-]', '_', name)
    # 合并连续下划线
    cleaned = re.sub(r'_+', '_', cleaned).strip('_')
    return cleaned or "unknown"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 Canvas LMS 抓取学生成绩，导出为三列 Excel 表格。"
    )

    # 配置文件
    parser.add_argument("--config", help="JSON 配置文件路径（例如 setting/run_config.json）")

    # Canvas 连接参数（可覆盖配置文件）
    parser.add_argument("--canvas-token", default=os.getenv("CANVAS_API_TOKEN"),
                        help="Canvas API token。默认读取 CANVAS_API_TOKEN 环境变量。")
    parser.add_argument("--canvas-url", default=os.getenv("CANVAS_BASE_URL", "https://oc.sjtu.edu.cn/api/v1"),
                        help="Canvas API 地址。")
    parser.add_argument("--canvas-course-id", default=os.getenv("CANVAS_COURSE_ID"),
                        help="Canvas 课程 ID。")
    parser.add_argument("--canvas-assignment-id", default=os.getenv("CANVAS_ASSIGNMENT_ID"),
                        help="Canvas 作业 ID。")

    # 抓取模式
    parser.add_argument("--mode", choices=["all", "graded", "submitted"], default="all",
                        help="抓取模式: all=所有学生, graded=有成绩的学生, submitted=已提交的学生。默认 all。")

    # 输出
    parser.add_argument("--output-dir", default="output", help="输出目录。默认 output。")

    args = parser.parse_args(argv)

    # 从配置文件加载
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        if not config_path.exists():
            raise SystemExit(f"配置文件不存在: {config_path}")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        # 配置文件中的字段名用下划线，与 CLI 参数一致
        if not args.canvas_token:
            args.canvas_token = config.get("canvas_api_token") or config.get("canvas_token")
        if args.canvas_url == "https://oc.sjtu.edu.cn/api/v1":
            args.canvas_url = config.get("canvas_api_url", args.canvas_url)
        if not args.canvas_course_id:
            args.canvas_course_id = config.get("canvas_course_id")
        if not args.canvas_assignment_id:
            args.canvas_assignment_id = config.get("canvas_assignment_id")

    # 验证必填参数
    if not args.canvas_token:
        raise SystemExit(
            "缺少 Canvas API token。请设置 CANVAS_API_TOKEN 环境变量，"
            "或通过 --canvas-token 传入，或在配置文件中指定 canvas_api_token。"
        )
    if not args.canvas_course_id:
        raise SystemExit("缺少课程 ID。请通过 --canvas-course-id 或配置文件指定。")
    if not args.canvas_assignment_id:
        raise SystemExit("缺少作业 ID。请通过 --canvas-assignment-id 或配置文件指定。")

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 创建 Canvas 客户端
    client = CanvasClient(args.canvas_url, args.canvas_token)

    try:
        mode_label = {"all": "所有学生", "graded": "有成绩的学生", "submitted": "已提交的学生"}
        print(f"模式: {mode_label[args.mode]}")
        print(f"课程 ID: {args.canvas_course_id}, 作业 ID: {args.canvas_assignment_id}")

        # 抓取成绩
        rows, assignment_name = fetch_grades(
            client,
            args.canvas_course_id,
            args.canvas_assignment_id,
            args.mode,
        )

        # 生成输出文件名
        safe_name = safe_filename(assignment_name)
        output_filename = f"canvas_grades_{safe_name}.xlsx"
        output_path = Path(args.output_dir) / output_filename

        # 写入 Excel
        write_xlsx(output_path, [("成绩", rows)])

        data_count = len(rows) - 1  # 减去表头
        print(f"\n✅ 已导出 {data_count} 条记录到: {output_path}")
        return 0

    except Exception as e:
        print(f"\n❌ 错误: {e}", file=sys.stderr)
        return 1

    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
