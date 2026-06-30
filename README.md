# AI 作业批改系统

一个基于 AI 视觉模型的自动作业批改工具，专为大学物理/理工科课程的 PDF 作业设计。支持从 ** Canvas 教学系统**（`oc.sjtu.edu.cn`）自动获取学生提交的 PDF、调用 OpenAI 兼容接口进行 AI 批改、生成详细的成绩报告，并自动将分数和评语上传回 Canvas。

---

## 目录

- [AI 作业批改系统](#ai-作业批改系统)
  - [目录](#目录)
  - [功能概览](#功能概览)
    - [核心批改能力](#核心批改能力)
    - [Canvas LMS 集成](#canvas-lms-集成)
    - [评价与安全](#评价与安全)
  - [项目文件说明](#项目文件说明)
  - [环境要求](#环境要求)
    - [安装 Python 依赖](#安装-python-依赖)
  - [快速开始](#快速开始)
    - [1. 安装依赖](#1-安装依赖)
    - [2. 配置 API](#2-配置-api)
    - [3. 独立批改（非 Canvas）](#3-独立批改非-canvas)
    - [4. Canvas 集成批改](#4-canvas-集成批改)
      - [4.1 创建配置文件](#41-创建配置文件)
      - [4.2 分步执行（推荐首次使用）](#42-分步执行推荐首次使用)
    - [4.3 导出 Canvas 成绩表](#43-导出-canvas-成绩表)
  - [配置文件详解](#配置文件详解)
  - [命令行参数](#命令行参数)
    - [独立批改参数](#独立批改参数)
    - [Canvas 集成参数](#canvas-集成参数)
    - [流程控制参数](#流程控制参数)
    - [成绩导出参数（`fetch_grades.py`）](#成绩导出参数fetch_gradespy)
  - [评分策略说明](#评分策略说明)
    - [1. 基础评分模式（`--grading-mode`）](#1-基础评分模式--grading-mode)
    - [2. 助教差异评分（默认启用）](#2-助教差异评分默认启用)
    - [3. 三道防线防止评分错误](#3-三道防线防止评分错误)
  - [Canvas 集成工作流程](#canvas-集成工作流程)
    - [学生身份映射机制](#学生身份映射机制)
    - [Canvas 评语格式](#canvas-评语格式)
    - [Canvas API 频率限制](#canvas-api-频率限制)
  - [输出文件](#输出文件)
  - [安全与隐私](#安全与隐私)
    - [不要提交到 GitHub 的内容](#不要提交到-github-的内容)
    - [API 密钥安全](#api-密钥安全)
    - [Canvas 上传安全](#canvas-上传安全)
  - [推荐使用流程](#推荐使用流程)
    - [首次使用（独立批改）](#首次使用独立批改)
    - [首次使用（Canvas 集成）](#首次使用canvas-集成)
    - [多助教分工](#多助教分工)
  - [常见问题与局限性](#常见问题与局限性)
    - [校园网代理问题](#校园网代理问题)
    - [上下文窗口限制](#上下文窗口限制)
    - [已知局限](#已知局限)
  - [License](#license)

---

## 功能概览

### 核心批改能力

- **AI 视觉批改**：将 PDF 渲染为图片后发送给视觉语言模型，支持手写、扫描件、电子 PDF
- **参考答案自动解析**：从参考答案 PDF 中提取题号、分值和标准解法
- **多种评分模式**：标准（standard）、宽松（lenient）、严格（strict）三种模式
- **总分池分配**：指定"常规题共 10 分、附加题共 2 分"，AI 自动按题均分
- **三档给分标准自动区分**：常规题极度宽松（写了过程+答案就接近满分）、附加题适度严格
- **二次审核**：被标记为需复核的题目，AI 会针对性地做出二次判断
- **统计异常检测**：基于 z-score 检测该学生某题得分是否显著低于全班均值，捕捉 AI"自信但错了"的评分

### Canvas LMS 集成

- **自动获取**：从 Canvas API 拉取课程花名册、学生提交的 PDF 附件
- **身份映射**：自动关联 Canvas user_id ↔ 学号（sis_user_id）↔ 姓名，支持本地花名册补充缺失学号
- **学生切片**：使用 Python 切片语法（如 `45:90`）分批批改，适合多位助教分工
- **自动上传**：将成绩和拟人化评语上传到 Canvas SpeedGrader
- **安全模式**：预览（dry-run）、仅下载、仅上传、跳过上传等多种控制选项

### 评价与安全

- **逐题反馈 + 置信度**：每题附带详细反馈和置信度，低置信度自动标记人工复核
- **拟人化 Canvas 评语**：上传到 Canvas 的评语自然简洁，只说哪几题有问题和简要原因，不包含分数
- **多道防线**：低置信度复核 + AI 二次审核 + 统计异常检测，防止 AI"自信地错判"

---

## 项目文件说明

| 文件 | 用途 |
|---|---|
| `run.sh` | 统一启动脚本，默认读取 `setting/run_config.json` |
| `src/grade_homework_skill_patch.py` | 核心批改脚本，可独立运行（不依赖 Canvas） |
| `src/canvas_integration.py` | Canvas LMS 集成模块，编排"获取 → 批改 → 上传"全流程 |
| `src/fetch_grades.py` | 从 Canvas 抓取成绩并导出三列 Excel（学号、姓名、成绩），支持按模式筛选 |
| `setting/run_config.template.json` | 运行配置模板，包含本地/Canvas 两种模式的常用选项说明 |
| `setting/env.template.sh` | API/Canvas 环境变量模板，复制为 `setting/api_env.sh` 后填写私密信息 |
| `SKILL.auto-homework-grader.patch.md` | Codex Skill 配置说明（供 AI 辅助使用时参考） |
| `.gitignore` | Git 忽略规则，排除 PDF / Excel / 成绩数据 |
| `README.md` | 本说明文档 |

---

## 环境要求

- **Python 3.10+**
- 可访问的 OpenAI / OpenAI 兼容接口（如 Qwen 等视觉模型）
- `pdftoppm`（仅 `chat-vision` 后端需要，用于 PDF 渲染为图片）
  - Windows 安装 TeX Live 后自带，或单独安装 [Poppler](https://poppler.freedesktop.org/)
  - Linux/macOS 通常已安装或可通过包管理器安装

### 安装 Python 依赖

```powershell
python -m pip install "openai>=2.0.0" httpx openpyxl
```

---

## 快速开始

### 1. 安装依赖

```bash
python -m pip install "openai>=2.0.0" httpx openpyxl
```

确认 `pdftoppm` 可用：

```bash
pdftoppm -v
```

### 2. 创建私密环境变量

复制模板：

```bash
cp setting/env.template.sh setting/api_env.sh
```

然后编辑 `setting/api_env.sh`：

```bash
export AI_GRADER_API_KEY="你的 AI API key"
export AI_GRADER_BASE_URL="学校或服务商的 /v1 地址"
export AI_GRADER_MODEL="模型名"

# 仅 Canvas 模式需要
export CANVAS_API_TOKEN="你的 Canvas token"
```

`setting/api_env.sh` 已被 `.gitignore` 忽略，不会上传 GitHub。

### 3. 创建运行配置

复制模板：

```bash
cp setting/run_config.template.json setting/run_config.json
```

然后编辑 `setting/run_config.json`。最常改的是：

| 字段 | 说明 |
|---|---|
| `mode` | `local` 本地文件批改；`canvas` 从 Canvas 下载/上传 |
| `answer` | 参考答案 PDF 路径 |
| `submissions` | 学生作业 ZIP/文件夹，仅 `local` 模式需要 |
| `roster` | 花名册，可选 |
| `output_dir` | 输出目录，默认建议 `output` |
| `canvas_course_id` / `canvas_assignment_id` | Canvas 模式需要 |
| `max_workers` / `requests_per_minute` | 并发和请求次数限速 |
| `render_dpi` / `max_render_pages` | PDF 转图片质量和页数上限 |

模板里的 `_template_note`、`_quick_start`、`_help_by_section` 都只是说明，程序会忽略。真正生效的是后面的顶层配置字段。

旧的 `grading_config.json` 不再是推荐入口；现在统一使用 `setting/run_config.json`。如果你有旧配置，可以把字段复制到新的 `setting/run_config.json`。

### 4. 运行

先检查文件识别，不调用 AI：

```bash
./run.sh --dry-run-discover
```

正式运行：

```bash
./run.sh
```

使用另一个配置文件：

```bash
./run.sh setting/hw4_config.json
```

`run.sh` 顶部只有启动相关设置：默认 config 路径、默认 env 路径和 Python 命令。数据文件、输出路径和批改参数都在 `setting/run_config.json` 里指定。

### Canvas 常用分步命令

`setting/run_config.json` 中设置 `"mode": "canvas"` 后，可以这样分步跑：

```bash
./run.sh --canvas-fetch-only
./run.sh --canvas-skip-upload
./run.sh --canvas-dry-run-upload
./run.sh
```

### 导出 Canvas 成绩表

独立脚本 `src/fetch_grades.py` 可以直接从 Canvas 抓取指定作业的成绩，生成简洁的三列 Excel（学号、姓名、成绩），适合存档或导入成绩系统。

```bash
python src/fetch_grades.py --config setting/run_config.json --mode all
python src/fetch_grades.py --config setting/run_config.json --mode graded
python src/fetch_grades.py --config setting/run_config.json --mode submitted
```

**三种抓取模式：**

| 模式 | 说明 |
|---|---|
| `all`（默认） | 所有选课学生，未提交/未评分的成绩列留空 |
| `graded` | 仅包含 Canvas 中已有成绩的学生 |
| `submitted` | 仅包含已提交作业的学生（含待评分） |

输出文件名格式：`canvas_grades_{作业名}.xlsx`，自动从 Canvas 获取作业名称。可通过 `--output-dir` 指定输出目录。

---

## 配置文件详解

`setting/run_config.json` 支持以下字段（均可选，未提供的用命令行参数或环境变量替代）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `canvas_course_id` | string | Canvas 课程 ID |
| `canvas_assignment_id` | string | Canvas 作业 ID |
| `answer` | string | 参考答案 PDF 的绝对路径 |
| `regular_points` | int | 常规题总分数（默认 10） |
| `bonus_points` | int | 附加题总分数（默认 2） |
| `grading_mode` | string | 评分模式：`standard` / `lenient` / `strict` |
| `backend` | string | AI 后端：`chat-vision`（推荐）/ `responses` |
| `model` | string | 模型名称（如 `qwen`） |
| `review_threshold` | float | 置信度低于此值自动标记复核（默认 0.65） |
| `score_decimals` | int | 分数四舍五入小数位数（默认 1） |
| `blank_review_scores` | bool | 需复核的学生成绩留空（默认 true） |
| `canvas_student_slice` | string | Python 切片，如 `"0:45"` 批改前 45 人 |
| `max_pdfs` | int | 最多批改人数限制 |
| `max_workers` | int | 并发批改 worker 数，默认 1 |
| `requests_per_minute` | float | AI 请求总速率上限，0 表示不限制 |
| `api_max_retries` | int | OpenAI SDK 自动重试次数，默认 0，避免单份作业失败时长时间卡住 |
| `refresh_answer_key` | bool | 忽略已有 `answer_key.json` 并重新抽取参考答案 |
| `output_dir` | string | 输出目录路径 |
| `roster` | string/null | 本地花名册 xlsx 路径（用于补充 Canvas 缺失的学号） |

---

## 命令行参数

### 独立批改参数

| 参数 | 说明 |
|---|---|
| `--answer` | 参考答案 PDF 路径 |
| `--submissions` | 学生作业 ZIP / 文件夹 / 单个 PDF |
| `--roster` | 可选花名册 xlsx（含学号、姓名列） |
| `--regular-points` | 常规题总分配分值（默认 10） |
| `--bonus-points` | 附加题总分配分值（默认 2） |
| `--grading-mode` | `standard` / `lenient` / `strict` |
| `--backend` | `chat-vision`（推荐）/ `responses` |
| `--model` | 模型名称 |
| `--api-timeout` | AI API 单次请求超时秒数（默认 120） |
| `--api-max-retries` | OpenAI SDK 自动重试次数，默认 0 |
| `--render-timeout` | PDF 渲染单文件超时秒数（默认 120） |
| `--review-threshold` | 复核置信度阈值（默认 0.75） |
| `--score-decimals` | 上传成绩小数位数（默认 2） |
| `--blank-review-scores` | 需复核的学生成绩留空 |
| `--max-pdfs` | 限制本轮新增批改人数；续跑时已完成结果不计入 |
| `--max-workers` | 并发批改 worker 数，默认 1 |
| `--requests-per-minute` | AI 请求总速率上限，0 表示不限制 |
| `--refresh-answer-key` | 忽略已有 `answer_key.json` 并重新抽取参考答案 |
| `--output-profile` | 输出模式：`compact`（默认）/ `full` |
| `--no-resume` | 不复用已有结果，强制重新批改全部提交 |
| `--dry-run-discover` | 仅检查文件识别，不调用 AI |
| `--verbose` | 显示逐文件渲染/API 调试日志 |
| `--no-ai-analysis` | 跳过 AI 班级分析 |
| `--no-trust-env` | 绕过系统代理（校园网常见问题） |

### Canvas 集成参数

| 参数 | 环境变量 | 说明 |
|---|---|---|
| `--canvas-token` | `CANVAS_API_TOKEN` | Canvas 访问令牌 |
| `--canvas-url` | `CANVAS_BASE_URL` | API 地址（默认 `https://oc.sjtu.edu.cn/api/v1`） |
| `--canvas-course-id` | `CANVAS_COURSE_ID` | 课程 ID |
| `--canvas-assignment-id` | `CANVAS_ASSIGNMENT_ID` | 作业 ID |
| `--config` | — | JSON 配置文件路径 |
| `--canvas-student-slice` | — | 学生切片，例如 `"0:45"`、`"45:90"` |
| `--roster` | — | 本地花名册 xlsx，用于补充学号 |

### 流程控制参数

| 参数 | 说明 |
|---|---|
| `--canvas-fetch-only` | 仅下载提交，不批改不上传 |
| `--canvas-upload-only` | 跳过批改，仅从已有 `results.json` 上传 |
| `--canvas-dry-run-upload` | 预览上传内容，不实际调用 API |
| `--canvas-skip-upload` | 批改并生成报告，但不上传到 Canvas |
| `--canvas-overwrite-grades` | 覆盖 Canvas 中已有成绩（默认跳过已评分的） |
| `--no-review-pass` | 跳过 AI 二次复核 |
| `--no-ta-scoring` | 禁用助教差异评分（常规宽松/附加严格） |

### 成绩导出参数（`fetch_grades.py`）

| 参数 | 环境变量 | 说明 |
| --- | --- | --- |
| `--config` | — | JSON 配置文件路径（读取 Canvas 连接信息） |
| `--canvas-token` | `CANVAS_API_TOKEN` | Canvas 访问令牌 |
| `--canvas-url` | `CANVAS_BASE_URL` | API 地址（默认 `https://oc.sjtu.edu.cn/api/v1`） |
| `--canvas-course-id` | `CANVAS_COURSE_ID` | 课程 ID |
| `--canvas-assignment-id` | `CANVAS_ASSIGNMENT_ID` | 作业 ID |
| `--mode` | — | 抓取模式：`all`（默认）/ `graded` / `submitted` |
| `--output-dir` | — | 输出目录（默认 `output`） |

---

## 评分策略说明

系统采用三层评分策略：

### 1. 基础评分模式（`--grading-mode`）

| 模式 | 行为 |
|---|---|
| `standard` | 按参考答案公平给分 |
| `lenient` | 更重视等价表达和解题思路，给更多过程分 |
| `strict` | 严格要求完整推导和最终形式 |

### 2. 助教差异评分（默认启用）

当使用 Canvas 集成模式时，会注入额外的评分规则（可通过 `--no-ta-scoring` 禁用）：

- **常规题 — 极度宽松**：写了推导过程 + 写了答案 → 接近满分（只扣 0.1~0.2 分），不论答案是否正确
- **扣分上限**：写了过程的题，扣分不超过该题满分的一半
- **正负号笔误**：步骤完整时不扣分，只当疑似乱猜才微扣 0.1 分
- **附加题 — 适度严格**：重点考察思路方向，答案错误扣 0.5 分

### 3. 三道防线防止评分错误

| 防线 | 机制 | 说明 |
|---|---|---|
| 置信度复核 | `review_threshold`（默认 0.65） | 低于阈值的题目自动标记 `needs_review` |
| AI 二次审核 | `review_flagged_questions()` | 被标记的题目再次送审，AI 针对性地重新判断 |
| 统计异常检测 | `detect_score_outliers()` | z-score 检测——某学生得分远低于全班均值时自动标记 |

---

## Canvas 集成工作流程

```
┌─────────────────────────────────────────────────────────┐
│  1. 获取课程花名册  →  Canvas user_id ↔ 学号 ↔ 姓名     │
│     merge_canvas_and_local_roster() 补充缺失学号          │
├─────────────────────────────────────────────────────────┤
│  2. 获取作业提交列表 → 筛选已提交、切片学生              │
├─────────────────────────────────────────────────────────┤
│  3. 下载 PDF 附件到本地临时目录                          │
├─────────────────────────────────────────────────────────┤
│  4. AI 抽取参考答案 → 逐学生批改 → 写入 results.json     │
│     run_grading_pipeline()                                │
├─────────────────────────────────────────────────────────┤
│  5. 二次审核 flagged 题目 → review_flagged_questions()   │
├─────────────────────────────────────────────────────────┤
│  6. 统计异常检测 → detect_score_outliers()                │
├─────────────────────────────────────────────────────────┤
│  7. 生成成绩表 → 逐题明细 → 人工复核表 → 班级分析        │
├─────────────────────────────────────────────────────────┤
│  8. 上传分数 + 拟人化评语到 Canvas                        │
│     PUT /submissions/:user_id  +  comment[text_comment]   │
└─────────────────────────────────────────────────────────┘
```

### 学生身份映射机制

Canvas 的提交数据可能不包含学号（`sis_user_id` 为空）。系统通过以下方式解决：

1. 先从 Canvas 花名册获取 `sis_user_id`
2. 如果为空，从 Canvas `enrollments` 数据中查找
3. 如果仍为空，读取本地花名册 xlsx，按**姓名**匹配填充学号

### Canvas 评语格式

上传到 Canvas 的评语会**自动拟人化**，不包含具体分数：

- 只提哪几题有问题 + 简短原因
- 去除 AI 常见的套话前缀（"基本正确，但…"等）
- 根据不同情况自动选择句式（1 题 / 2-3 题 / 多题）

示例：
```
第2题有点小问题——公式对但计算错了。其他题都挺好。
第1题推导不完整；第3题单位漏写。其余没问题。
```

> 注意：本地的 `批改明细.xlsx` 和 `批改详情.md` 中的反馈是完整版，包含详细评分和所有题目的完整反馈。

### Canvas API 频率限制

Canvas 采用漏桶算法限流。脚本内置了：
- 监控 `X-Rate-Limit-Remaining` 响应头
- 余量低于 50 时自动加 1 秒延迟
- 遇到 403 频率限制时指数退避重试（最多 3 次）

---

## 输出文件

批改完成后，默认 `compact` 输出包含以下文件：

| 文件 | 内容 |
|---|---|
| `总成绩_三列表.xlsx` | 学号、姓名、成绩——适合直接上传成绩系统 |
| `批改明细.xlsx` | 每题得分/满分、置信度、逐题反馈、复核标记 |
| `人工复核.xlsx` | 需要人工检查的学生名单和原因 |
| `answer_key.json` | AI 抽取的参考答案结构化数据 |
| `answer_key_meta.json` | 参考答案 PDF 指纹和配分参数，用于判断缓存是否可复用 |
| `AI请求速率分析.md` | AI 请求次数、平均速率和限流等待统计 |
| `作业耗时诊断.md` | 每份作业的总耗时、渲染耗时、AI 请求耗时和异常原因 |
| `results.json` | 完整的结构化批改结果 |

运行中会实时写入 `partial_results.json` 作为中断备份；`compact` 模式正常完成后会删除它，下一次续跑使用 `results.json`。

加 `--output-profile full` 时额外生成：

| 文件 | 内容 |
|---|---|
| `批改详情.md` | 面向助教的详细批改报告 |
| `班级分析.md` | 班级整体表现与常见问题分析 |
| `AI请求速率分析.json` | AI 请求速率分析的结构化版本 |
| `作业耗时诊断.json` | 作业耗时诊断的结构化版本 |

---

## 安全与隐私

### 不要提交到 GitHub 的内容

已在 `.gitignore` 中排除，请确认上传前已添加：

```gitignore
*.pdf          # 学生作业、参考答案
*.zip          # 作业压缩包
*.xlsx         # 成绩表（含学号、姓名、分数）
output/        # 默认批改输出
grading_output*/   # 旧版批改输出
__pycache__/
*.pyc
.env           # 环境变量
setting/api_env.sh    # 私密 API/Canvas 环境变量
setting/run_config.json   # 个人运行配置
*.key          # 密钥文件
.DS_Store
Thumbs.db
```

### API 密钥安全

- **绝不**将 API key 硬编码在脚本中或写入 JSON 配置文件
- 使用环境变量传递密钥（`$env:AI_GRADER_API_KEY`、`$env:CANVAS_API_TOKEN`）
- `.gitignore` 已排除 `.env`、`setting/api_env.sh` 和个人运行配置

### Canvas 上传安全

- 默认**跳过** Canvas 中已有成绩的提交（防止覆盖人工评分）
- 使用 `--canvas-overwrite-grades` 才会覆盖已有成绩
- 使用 `--canvas-dry-run-upload` 可以预览上传内容而不实际写入

---

## 推荐使用流程

### 首次使用（独立批改）

1. 配置 API 环境变量
2. 用 `--dry-run-discover` 检查文件识别情况
3. 用 `--max-pdfs 1` 测试批改一个学生
4. 检查 `批改明细.xlsx` 和 `人工复核.xlsx`，确认评分合理
5. 去掉限制，正式跑全班
6. 人工处理复核表中的学生
7. 使用三列成绩表上传

### 首次使用（Canvas 集成）

1. 获取 Canvas API Token
2. 创建 `setting/run_config.json`，填写课程和作业 ID，并设置 `"mode": "canvas"`
3. `--canvas-fetch-only` → 确认能下载
4. `--canvas-skip-upload` → 批改并检查结果
5. `--canvas-dry-run-upload` → 预览上传内容
6. 无参数 → 正式批改并上传
7. 去 Canvas SpeedGrader 抽查 3-5 个学生，确认分数和评语正确

### 多助教分工

每个助教使用不同的 `canvas_student_slice` 配置：

```json
// 助教 A：前 45 人
"canvas_student_slice": "0:45"

// 助教 B：后 45 人
"canvas_student_slice": "45:90"
```

各自运行后在 Canvas 上各自负责的学生范围内可见成绩。

---

## 常见问题与局限性

### 校园网代理问题

如果遇到 TLS/SSL 连接错误（`EOF occurred in violation of protocol`），说明 httpx 读取了系统代理设置。Canvas 集成脚本已默认设置 `trust_env=False` 绕过系统代理。独立批改脚本可加 `--no-trust-env`。

### 上下文窗口限制

每名学生的批改是独立的 API 调用，参考答案会完整注入每次请求的 prompt 中。但单次调用内，长 prompt + 多页 PDF 图片仍可能超出模型的上下文窗口。建议：
- 控制每份作业的页数（脚本默认渲染前 12 页）
- 如果作业页数较多，可考虑仅批改关键题目

### 已知局限

- AI 批改结果**不是最终成绩**，尤其是零分、低置信度、题目匹配异常时必须人工复核
- 手写内容过暗、截断、旋转、模糊时模型可能误读
- 不同 OpenAI 兼容提供方对图片输入和 JSON 输出的支持程度不同
- 参考答案 PDF 需要题号标注清晰，模糊的标注可能影响题目识别

---

---

## License

本项目仅供教学和个人使用。如需公开发布，请补充正式的 License 文件（如 MIT License）。
