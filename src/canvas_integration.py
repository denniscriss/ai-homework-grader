#!/usr/bin/env python3
"""Canvas LMS integration for AI Homework Grader.

Fetches student PDF submissions from SJTU Canvas (oc.sjtu.edu.cn), runs the
existing AI grading pipeline, and uploads scores + feedback comments back.

Usage:
  # Full pipeline: fetch -> grade -> upload
  python src/canvas_integration.py --config setting/run_config.json

  # Fetch submissions only (verify downloads work)
  python src/canvas_integration.py --config setting/run_config.json --canvas-fetch-only

  # Grade but don't upload (review before publishing)
  python src/canvas_integration.py --config setting/run_config.json --canvas-skip-upload

  # Upload from existing results.json (re-upload after fixing)
  python src/canvas_integration.py --config setting/run_config.json --canvas-upload-only

  # Preview what would be uploaded without actually sending
  python src/canvas_integration.py --config setting/run_config.json --canvas-dry-run-upload
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

# Reuse the grading core from the existing script
from grade_homework_skill_patch import (
    ApiRateLimiter,
    AIBackend,
    answer_key_schema,
    apply_point_allocation,
    error_result,
    filename_identity,
    grading_schema,
    load_or_extract_answer_key,
    load_existing_results,
    make_ai_backend,
    norm_id,
    norm_text,
    normalize_result,
    read_roster,
    run_grading_pipeline,
    sanitize_for_output,
    write_class_analysis_md,
    clean_compact_output_files,
    write_clean_grades,
    write_details_md,
    write_details_xlsx,
    write_rate_analysis_files,
    write_submission_diagnostics,
    write_review_xlsx,
)

# ---------------------------------------------------------------------------
# Canvas API client
# ---------------------------------------------------------------------------

CANVAS_PER_PAGE = 100  # Max items per paginated request
RATE_LIMIT_LOW = 50    # Start throttling when remaining drops below this
MAX_RETRIES = 3        # Max retries on rate-limit 403


class CanvasClient:
    """Minimal Canvas LMS REST API client over httpx."""

    def __init__(self, base_url: str, api_token: str, trust_env: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=60.0,
            trust_env=trust_env,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Make a single API request with retry + rate-limit awareness."""
        url = f"{self.base_url}{path}"
        for attempt in range(MAX_RETRIES):
            resp = self._client.request(method, url, json=json_data, data=data, params=params)

            # Rate-limit check
            remaining = resp.headers.get("X-Rate-Limit-Remaining")
            if remaining is not None:
                try:
                    if float(remaining) < RATE_LIMIT_LOW:
                        time.sleep(1.0)
                except ValueError:
                    pass

            if resp.status_code == 403 and "Rate Limit Exceeded" in resp.text:
                wait = 2 ** (attempt + 1)
                print(f"  Rate limited. Waiting {wait}s before retry {attempt + 1}/{MAX_RETRIES}...")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        resp.raise_for_status()
        return resp  # unreachable, but keeps type checker happy

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Fetch all pages for a list endpoint."""
        params = dict(params or {})
        params.setdefault("per_page", CANVAS_PER_PAGE)
        all_items: list[dict[str, Any]] = []
        page = 1
        while True:
            params["page"] = page
            resp = self._request("GET", path, params=params)
            items = resp.json()
            if not items:
                break
            all_items.extend(items)
            # Check Link header for next page as a safety net
            link = resp.headers.get("Link", "")
            if 'rel="next"' not in link and len(items) < CANVAS_PER_PAGE:
                break
            page += 1
        return all_items

    # ------------------------------------------------------------------
    # Course roster
    # ------------------------------------------------------------------

    def get_course_roster(self, course_id: int | str) -> list[dict[str, Any]]:
        """Fetch enrolled students for a course.

        Returns a list of dicts with keys:
            canvas_user_id, sis_user_id, name, login_id, sortable_name
        """
        path = f"/courses/{course_id}/users"
        params = {
            "enrollment_type[]": "student",
            "include[]": ["email", "enrollments"],
        }
        raw = self._paginate(path, params)
        roster: list[dict[str, Any]] = []
        for user in raw:
            sis_id = user.get("sis_user_id") or ""
            login_id = user.get("login_id") or ""
            # sis_user_id may also be nested inside enrollments array
            if not sis_id:
                for enrollment in user.get("enrollments") or []:
                    sis_id = enrollment.get("sis_user_id") or ""
                    if sis_id:
                        break
            # If sis_user_id is still missing, try to extract digits from login_id
            if not sis_id and login_id:
                digits = re.sub(r"\D", "", login_id)
                if len(digits) >= 5:
                    sis_id = digits
            roster.append({
                "canvas_user_id": user["id"],
                "sis_user_id": sis_id,
                "name": user.get("name", ""),
                "sortable_name": user.get("sortable_name", ""),
                "login_id": login_id,
            })
        print(f"Canvas roster: {len(roster)} students enrolled")
        return roster

    # ------------------------------------------------------------------
    # Submissions & attachments
    # ------------------------------------------------------------------

    def get_submissions(
        self,
        course_id: int | str,
        assignment_id: int | str,
    ) -> list[dict[str, Any]]:
        """Fetch all submissions for an assignment.

        Each returned dict includes the submission fields plus:
          - 'canvas_user_id' (from submission['user_id'])
          - 'user_name' (from included user object)
          - 'attachments' (list of file objects, if online_upload)
        """
        path = f"/courses/{course_id}/assignments/{assignment_id}/submissions"
        params: dict[str, Any] = {
            "include[]": ["user", "submission_history"],
        }
        raw = self._paginate(path, params)

        submissions: list[dict[str, Any]] = []
        for sub in raw:
            user = sub.get("user") or {}
            submissions.append({
                "canvas_user_id": sub.get("user_id"),
                "user_name": user.get("name", ""),
                "assignment_id": sub.get("assignment_id"),
                "workflow_state": sub.get("workflow_state", ""),
                "submitted_at": sub.get("submitted_at"),
                "score": sub.get("score"),
                "grade": sub.get("grade"),
                "attempt": sub.get("attempt"),
                "late": sub.get("late", False),
                "excused": sub.get("excused", False),
                "missing": sub.get("missing", False),
                "attachments": sub.get("attachments") or [],
            })

        submitted = [s for s in submissions if s["workflow_state"] not in ("unsubmitted",)]
        print(f"Canvas submissions: {len(submissions)} total, {len(submitted)} with submissions")
        return submissions

    def download_attachment(
        self,
        attachment: dict[str, Any],
        target_dir: Path,
        preferred_name: str = "",
    ) -> Path | None:
        """Download a single attachment to *target_dir*.

        *preferred_name* is used as the local filename stem (without extension).
        Returns the Path of the downloaded file, or None on failure.
        """
        file_url = attachment.get("url", "")
        if not file_url:
            return None

        # Canvas attachment URLs can be relative paths (e.g. /files/569/download?download_frd=1)
        # or full URLs. Prepend the Canvas domain if the URL is relative.
        if not file_url.startswith(("http://", "https://")):
            # self.base_url is e.g. https://oc.sjtu.edu.cn/api/v1
            # We need https://oc.sjtu.edu.cn as the domain
            from urllib.parse import urlparse
            parsed = urlparse(self.base_url)
            domain = f"{parsed.scheme}://{parsed.netloc}"
            file_url = domain + file_url

        display_name = attachment.get("display_name") or attachment.get("filename") or "submission.pdf"
        ext = Path(display_name).suffix or ".pdf"
        if ext.lower() not in (".pdf",):
            ext = ".pdf"
        local_name = (preferred_name or Path(display_name).stem) + ext
        local_path = target_dir / local_name

        # Convert httpx.Headers to plain dict for standalone httpx.get() call
        auth_headers = dict(self._client.headers)
        try:
            resp = httpx.get(
                file_url,
                headers=auth_headers,
                timeout=120.0,
                follow_redirects=True,
                trust_env=False,
            )
            if resp.status_code >= 400:
                print(f"  HTTP {resp.status_code} downloading {display_name}")
                return None
        except Exception as exc:
            print(f"  Failed to download {display_name}: {exc}")
            return None

        target_dir.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(resp.content)
        return local_path

    # ------------------------------------------------------------------
    # Grade upload
    # ------------------------------------------------------------------

    def put_grade(
        self,
        course_id: int | str,
        assignment_id: int | str,
        user_id: int | str,
        score: float | str,
        comment: str = "",
    ) -> dict[str, Any]:
        """Upload a grade and optional comment for one student.

        Returns the API response dict.
        """
        path = f"/courses/{course_id}/assignments/{assignment_id}/submissions/{user_id}"
        data: dict[str, Any] = {
            "submission[posted_grade]": str(score),
        }
        if comment.strip():
            data["comment[text_comment]"] = comment.strip()
        resp = self._request("PUT", path, data=data)
        return resp.json()

    def put_grade_batch(
        self,
        course_id: int | str,
        assignment_id: int | str,
        grade_data: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Upload grades in bulk.

        Each item in *grade_data* should have:
            canvas_user_id, score, comment (optional)
        """
        path = f"/courses/{course_id}/assignments/{assignment_id}/submissions/update_grades"
        # The bulk endpoint expects grade_data[<user_id>][posted_grade] and
        # grade_data[<user_id>][text_comment]
        payload: dict[str, dict[str, str]] = {}
        for item in grade_data:
            uid = str(item["canvas_user_id"])
            payload[uid] = {"posted_grade": str(item["score"])}
            comment = (item.get("comment") or "").strip()
            if comment:
                payload[uid]["text_comment"] = comment

        resp = self._request("POST", path, data={"grade_data": json.dumps(payload)})
        # The bulk endpoint may not return detailed per-student results; check status
        result = resp.json() if resp.text else {}
        return result if isinstance(result, list) else [result]

    # ------------------------------------------------------------------
    # Assignment metadata
    # ------------------------------------------------------------------

    def get_assignment(
        self,
        course_id: int | str,
        assignment_id: int | str,
    ) -> dict[str, Any]:
        """Fetch metadata for a single assignment (name, points_possible, etc.)."""
        path = f"/courses/{course_id}/assignments/{assignment_id}"
        return self._request("GET", path).json()


# ---------------------------------------------------------------------------
# Identity mapping
# ---------------------------------------------------------------------------

def build_canvas_roster_for_grading(
    canvas_roster: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert Canvas roster into the format expected by the grading pipeline.

    The returned list of dicts has 'student_id', 'name', and 'canvas_user_id'
    keys. This list can be passed as the *roster* argument to
    run_grading_pipeline() and normalize_result().
    """
    return [
        {
            "student_id": str(row["sis_user_id"]),
            "name": str(row["name"]),
            "canvas_user_id": row["canvas_user_id"],
        }
        for row in canvas_roster
    ]


def merge_canvas_and_local_roster(
    canvas_roster: list[dict[str, Any]],
    local_roster: list[dict[str, str]] | None,
) -> list[dict[str, Any]]:
    """Merge Canvas roster data with a local xlsx roster to fill missing student IDs.

    Canvas roster provides: canvas_user_id, name, (sometimes empty) sis_user_id.
    Local roster provides: student_id (学号), name.
    We match by normalized name, and fill in student_id from the local roster
    when Canvas sis_user_id is empty.
    """
    grading_roster = build_canvas_roster_for_grading(canvas_roster)

    if not local_roster:
        return grading_roster

    # Build lookup from local roster by normalized name
    local_by_name: dict[str, str] = {}
    local_by_id: dict[str, str] = {}
    for row in local_roster:
        sid = str(row.get("student_id", "")).strip()
        name = str(row.get("name", "")).strip()
        if name:
            local_by_name[norm_text(name)] = sid
        if sid:
            local_by_id[norm_id(sid)] = sid

    filled_count = 0
    for entry in grading_roster:
        if entry["student_id"]:
            continue  # Already has 学号 from Canvas

        # Try matching by name first
        name_key = norm_text(str(entry.get("name", "")))
        if name_key in local_by_name:
            entry["student_id"] = local_by_name[name_key]
            filled_count += 1
            continue

        # Try matching by partial name (e.g. Canvas has full name, roster has short)
        for local_name, local_sid in local_by_name.items():
            if local_name and local_name in name_key:
                entry["student_id"] = local_sid
                filled_count += 1
                break

    if filled_count:
        print(f"Local roster: filled {filled_count} missing student IDs by name matching")
    return grading_roster


def _humanize_feedback(feedback: str) -> str:
    """Extract a short, human-friendly error description from AI feedback."""
    import re as _re
    f = feedback.strip()
    # Remove common AI hedging prefixes — keep only the actual issue
    for prefix in ["基本参数正确。但", "思路正确。但", "推导正确。但", "整体思路对，但",
                    "基本正确，但", "大体正确，但", "大体上正确，但",
                    "基本参数正确。但在", "思路正确。但在"]:
        if f.startswith(prefix):
            f = f[len(prefix):]
            break
    # Remove filler phrases
    f = _re.sub(r"在[写作]*[一-鿿A-Za-z]+时[，,]\s*", "", f)
    # Strip trailing punctuation to avoid double-period
    f = f.rstrip("，,。；;：:！!")
    # Trim to ~35 chars
    if len(f) > 35:
        f = f[:32] + "..."
    return f or "有小错误"


def format_feedback_comment(
    result: dict[str, Any],
    answer_key: dict[str, Any],
) -> str:
    """Build a human-like Canvas submission comment. Natural tone, no AI flavor."""
    # Collect problem questions (lost > 20% or flagged)
    problem_qids: list[str] = []
    for q in result.get("questions", []):
        score = q.get("score", 0)
        max_q = q.get("max_points", 0)
        ratio = score / max_q if max_q > 0 else 1.0
        if max_q > 0 and (ratio < 0.8 or q.get("needs_review")):
            qid = str(q.get("id", "")).replace("第", "").replace("题", "")
            problem_qids.append(qid)

    if not problem_qids:
        return "都对了，做得不错~"

    qids = "、".join(problem_qids)
    return f"第{qids}题有问题，请查看。"


# ---------------------------------------------------------------------------
# Differentiated scoring rules
# ---------------------------------------------------------------------------

def build_extra_scoring_rules() -> str:
    """Extra scoring rules for TA grading: lenient on regular, strict on bonus."""
    return """
极其重要的评分准则（覆盖所有题目，无论评分模式，必须严格遵守）:

【核心原则：本质等价即满分】
- 数学上等价的不同表达式必须给满分。例如：系数用 4A 和标准答案中的 2A=0.1 可能是同一物理量的不同写法、三角函数的不同相位表达、指数/对数的等价变换、坐标变换后的等价形式。看到形式差异时，必须先判断是否数学等价，等价则半分不扣。
- 不同的推导方法、不同的求解路径，只要物理思路正确、结论一致，必须给满分。不要因为"标准答案用了方法A、学生用了方法B"而扣分。
- 学生写得"繁琐"不等于"错误"。步骤多、表达式长、绕了弯路但最终正确，都是满分。

【常规题（type=regular）: 极度宽松，重点看"学生是否认真做了"】

【扣分上限原则 — 最重要，先读】
- 任何题目，只要学生写了过程（无论正误），扣分不得超过该题满分的一半。
  即：这道题的最低得分 = 满分 × 0.5。
- 例外：完全空白、或只写了一个孤立的错误数字且无任何推导步骤 → 才给 0 分。

【写了过程 + 写了答案 → 答案正确满分，答案错误至少扣 0.1 分】
- 只要学生的作答包含推导步骤和最终答案（两个都有），需要判断答案是否正确。
- 答案数学上等价于标准答案 → 满分，不扣分。
- 答案不等价（即答案错误）→ 必须至少扣 0.1 分，无论思路是否正确。扣分范围 0.1~0.2。
- 判断标准：学生有没有写过程？有没有写答案？两个都有且答案正确 → 满分；两个都有但答案错误 → 至少扣 0.1。
- 例子：学生写满了一张纸的推导，最后答案算错了 → 扣 0.1，给 4.9/5.0。

【正负号错误 — 宽松但必须扣分】
- 正负号写错（如 + 写成 -、sin 写成 -sin、相位多/少 π 等）属于答案错误，必须至少扣 0.1 分。
  * 如果学生的步骤完整、推导清晰 → 扣 0.1 分。符号极可能是笔误，但答案错误必须扣分。
  * 如果步骤跳跃、推导不完整、疑似乱猜 → 扣 0.1 分。
- 判断标准：无论步骤多少，正负号错误一律扣 0.1。

【其他不扣分的情况】
- 系数形式不同但等价、相位写法不同、用了不同坐标系、答案未化简、单位漏写（数值正确）、用近似值代替精确值 → 一律不扣分。
- 不同推导方法但结果是等价表达 → 满分。

【常规题评分速查表】
| 学生情况 | 该给几分 |
|---|---|
| 有过程、有答案、结果正确 | 满分 |
| 有过程、有答案、结果不对 | 满分 - 0.1~0.2（答案错误必须扣至少 0.1） |
| 有过程、有答案、正负号错了但步骤多 | 满分 - 0.1（正负号错误也是答案错误，必须扣分） |
| 有过程、有答案、正负号错了步骤少 | 满分 - 0.1 |
| 有过程、没写最终答案 | 满分 - 0.3 |
| 只写了一个答案数字，没有过程 | 满分 × 0.5 |
| 完全空白 | 0 分 |

【附加题（type=bonus）: 适度严格】
- 重点考察思路方向。思路对但答案有误，扣 0.5 分；思路基本对但推导不完整，扣 0.3 分。
- 答案完全错误且思路不对时，仍应给 0.5-1 分的过程分（如果写了相关内容）。

【置信度】
- 当学生答案与标准答案形式不同但你判断为等价的，confidence 设为 0.85-0.95。
- 当你不确定是否等价时，confidence 设为 0.7-0.8，needs_review=true，但分数给偏高的一边。

【强制自检步骤 — 每道题评分前必须执行】
1. 先看学生有没有写推导过程？有过程 → 跳到第 3 条。
2. 没过程、只有答案数字 → 给满分的 50%。
3. 有过程、也有答案 → 看答案是否数学上等价于标准答案？等价 → 满分。
4. 不等价 → 答案错误，必须至少扣 0.1 分。然后看正负号：正负号错但步骤多 → 扣 0.1；步骤少 → 扣 0.1。
5. 正负号对但答案其他部分错 → 扣 0.1~0.2（上限！不要多扣）。
6. 最后自问: 这个学生认真做了吗？认真做的迹象（写了过程、写了答案）→ 给满分或接近满分（但答案错误时至少扣 0.1）。

【总体】
- 评分时反复自问"这个学生懂不懂这道题？"如果答案是"懂"，就给满分或接近满分。
- 永远不要因为"写法和标准答案不一样"而扣分。
- **confidence 打分**: 如果你对自己的判断有任何一丝犹豫，confidence 设 0.6-0.7 并标 needs_review——宁可多标 review，不要放过错误评分。
"""


# ---------------------------------------------------------------------------
# Second-pass AI review for flagged questions
# ---------------------------------------------------------------------------

def review_flagged_questions(
    ai: AIBackend,
    results: list[dict[str, Any]],
    answer_key: dict[str, Any],
    extra_scoring_rules: str,
    rate_limiter: ApiRateLimiter | None = None,
) -> list[dict[str, Any]]:
    """Re-evaluate questions flagged as needs_review with a focused second AI pass.

    Only reviews questions where needs_review=True. Sends the original evidence
    and first-pass score to the AI for reconsideration.
    """
    # Build per-question lookup from answer_key
    key_questions = {str(q.get("id", "")): q for q in answer_key.get("questions", [])}

    def is_question_review_reason(reason: Any) -> bool:
        prefix, separator, _ = str(reason).partition(":")
        return bool(separator) and prefix.strip() in key_questions

    review_count = 0
    for result in results:
        for q in result.get("questions", []):
            if not q.get("needs_review"):
                continue

            qid = str(q.get("id", ""))
            key_q = key_questions.get(qid)
            if not key_q:
                continue

            max_pts = q.get("max_points", 0)
            if max_pts <= 0:
                continue

            old_score = q.get("score", 0)
            evidence = q.get("evidence", "").strip()
            old_feedback = q.get("feedback", "").strip()
            review_reason = q.get("review_reason", "").strip()

            print(f"  [Review] {result.get('name', '?')} Q{qid}: "
                  f"first pass {old_score}/{max_pts} (reason: {review_reason[:60]})")

            prompt = f"""你是审查助教。请重新评判下面这道题目的得分。

原评分规则:
{extra_scoring_rules.strip()}

题目 {qid}（满分 {max_pts} 分，类型 {key_q.get('type', 'unknown')}）:
参考解答: {key_q.get('reference_solution', '无')[:500]}
评分要点: {'; '.join(key_q.get('scoring_points', [])[:5])}

学生作业中可见的证据: {evidence if evidence else '无（题目未在作业中找到）'}

第一次评分: {old_score}/{max_pts}
第一次反馈: {old_feedback}
复核触发原因: {review_reason}

请重新给出你的评分（0 到 {max_pts} 之间，可以是小数）和新置信度。
如果第一次评分合理，可以维持原判。
输出 JSON: {{"score": 数字, "confidence": 数字(0-1), "reasoning": "重新判分理由(中文,100字以内)"}}
"""
            try:
                if rate_limiter:
                    rate_limiter.wait("review")
                text = ai.text(prompt)
                # Parse JSON from response
                obj = _extract_json(text)
                new_score = float(obj.get("score", old_score))
                new_score = max(0.0, min(float(max_pts), new_score))
                new_confidence = float(obj.get("confidence", q.get("confidence", 0.5)))
                new_confidence = max(0.0, min(1.0, new_confidence))
                reasoning = str(obj.get("reasoning", ""))[:120]

                if abs(new_score - old_score) > 0.01:
                    q["score"] = round(new_score, 2)
                    q["confidence"] = round(new_confidence, 2)
                    q["feedback"] = (old_feedback + " [复审调整: " + reasoning + "]").strip()
                    print(f"    Adjusted: {old_score} -> {new_score}/{max_pts}")
                else:
                    q["confidence"] = round(max(q.get("confidence", 0.5), new_confidence), 2)
                    print(f"    Confirmed: {old_score}/{max_pts} maintained")

                # If still needs review, keep the flag; otherwise clear if confidence improved
                if new_confidence >= 0.75:
                    q["needs_review"] = False
                    q["review_reason"] = ""
                    print(f"    Review flag cleared (confidence {new_confidence:.2f})")

                review_count += 1
            except Exception as exc:
                print(f"    Review failed: {exc} — keeping original score")

    if review_count:
        print(f"Second-pass review: {review_count} questions re-evaluated")

    # Recompute total scores after adjustments
    for result in results:
        questions = result.get("questions", [])
        regular = sum(float(q.get("score", 0) or 0) for q in questions if q.get("type") != "bonus")
        bonus = sum(float(q.get("score", 0) or 0) for q in questions if q.get("type") == "bonus")
        result["regular_score"] = round(regular, 2)
        result["bonus_score"] = round(bonus, 2)
        result["total_score"] = round(regular + bonus, 2)

        # Rebuild question-level review reasons from current question flags.
        # Preserve non-question reasons such as identity mismatches.
        preserved_reasons = [
            str(reason)
            for reason in result.get("review_reasons", [])
            if reason and not is_question_review_reason(reason)
        ]
        current_question_reasons = []
        for q in questions:
            if not q.get("needs_review"):
                continue
            qid = str(q.get("id", ""))
            reason = str(q.get("review_reason", "")).strip() or "still needs manual review"
            current_question_reasons.append(f"{qid}: {reason}")

        result["review_reasons"] = sorted(set(preserved_reasons + current_question_reasons))
        result["needs_review"] = bool(result["review_reasons"]) or any(q.get("needs_review") for q in questions)

    return results


def _extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from AI text response."""
    import json as _json
    cleaned = text.strip()
    # Try direct parse
    try:
        return _json.loads(cleaned)
    except Exception:
        pass
    # Try markdown fence
    match = __import__("re").search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=__import__("re").S)
    if match:
        return _json.loads(match.group(1))
    # Try find braces
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        return _json.loads(cleaned[start:end + 1])
    raise ValueError("No JSON found in response")


# ---------------------------------------------------------------------------
# Identity mapping
# ---------------------------------------------------------------------------


def resolve_canvas_user_id(
    result: dict[str, Any],
    roster: list[dict[str, Any]],
) -> int | None:
    """Find the Canvas user_id for a graded result.

    Tries multiple strategies:
    1. From result['canvas_user_id'] if present (stored during download)
    2. From roster match by student_id (digit comparison)
    3. From roster match by name (text comparison)
    4. From filename (extract canvas_ prefix)
    """
    # Strategy 1: explicitly stored
    if result.get("canvas_user_id"):
        return int(result["canvas_user_id"])

    sid = result.get("student_id", "")
    name = result.get("name", "")
    filename = result.get("filename", "")

    # Strategy 4: parse canvas_XXXXX_ prefix from filename
    m = re.match(r"canvas_(\d+)_", filename)
    if m:
        return int(m.group(1))

    # Strategy 2: match by student_id
    if sid:
        sid_digits = re.sub(r"\D", "", str(sid))
        for row in roster:
            if re.sub(r"\D", "", str(row.get("student_id", ""))) == sid_digits:
                return row.get("canvas_user_id")
            if re.sub(r"\D", "", str(row.get("sis_user_id", ""))) == sid_digits:
                return row.get("canvas_user_id")

    # Strategy 3: match by name
    if name:
        name_norm = re.sub(r"\s+", "", str(name)).lower()
        for row in roster:
            if re.sub(r"\s+", "", str(row.get("name", ""))).lower() == name_norm:
                return row.get("canvas_user_id")

    return None


# ---------------------------------------------------------------------------
# Statistical outlier detection — catch confidently-wrong AI grades
# ---------------------------------------------------------------------------

def detect_score_outliers(results: list[dict[str, Any]], std_threshold: float = 1.5) -> int:
    """Flag student-question scores that deviate significantly from class average.

    If a student's score on a question is more than *std_threshold* standard
    deviations from the mean, and it's not already flagged for review, add a
    review flag. This catches cases where the AI was confidently wrong.
    """
    if len(results) < 3:
        return 0  # Not enough data for statistical comparison

    # Collect per-question scores across all students
    from statistics import mean, stdev as std_dev
    question_scores: dict[str, list[tuple[int, float]]] = {}
    for result in results:
        for q in result.get("questions", []):
            qid = str(q.get("id", ""))
            if qid not in question_scores:
                question_scores[qid] = []
            question_scores[qid].append((results.index(result), q.get("score", 0)))

    flagged = 0
    for qid, scores in question_scores.items():
        values = [s[1] for s in scores]
        if len(values) < 3:
            continue
        try:
            avg = mean(values)
            std = std_dev(values)
        except Exception:
            continue
        if std < 0.01:
            continue  # All scores identical, no outliers

        for idx, score in scores:
            z = abs(score - avg) / std if std > 0 else 0
            if z > std_threshold and score < avg:
                # Student scored significantly below mean — possible grading error
                result = results[idx]
                q = result["questions"][next(i for i, qq in enumerate(result.get("questions", [])) if str(qq.get("id", "")) == qid)]
                if not q.get("needs_review"):
                    q["needs_review"] = True
                    old_reason = q.get("review_reason", "")
                    new_reason = f"统计异常: 得分{score}远低于班级均值{avg:.1f} (z={z:.1f})"
                    q["review_reason"] = (old_reason + "; " + new_reason).strip("; ")
                    result["needs_review"] = True
                    result.setdefault("review_reasons", []).append(f"{qid}: {new_reason}")
                    flagged += 1
                    print(f"  [Outlier] {result.get('name', '?')} Q{qid}: {score} vs class avg {avg:.1f}±{std:.1f}")

    if flagged:
        print(f"Statistical check: {flagged} outlier scores flagged for review")
    return flagged


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def _regenerate_output_files(output_dir: Path, args: argparse.Namespace) -> int:
    """Regenerate all Excel/MD output files from existing results.json and answer_key.json."""
    results_path = output_dir / "results.json"
    answer_key_path = output_dir / "answer_key.json"

    if not results_path.exists():
        raise SystemExit(f"results.json not found at {results_path}")
    if not answer_key_path.exists():
        raise SystemExit(f"answer_key.json not found at {answer_key_path}")

    results = sanitize_for_output(json.loads(results_path.read_text(encoding="utf-8")))
    answer_key = json.loads(answer_key_path.read_text(encoding="utf-8"))
    local_roster = read_roster(args.roster) if args.roster else None

    print(f"Loaded {len(results)} results from {results_path}")
    print(f"Regenerating output files in {output_dir}...")

    write_clean_grades(
        output_dir / "总成绩_三列表.xlsx", results, local_roster, args.blank_review_scores
    )
    write_details_xlsx(output_dir / "批改明细.xlsx", results)
    write_review_xlsx(output_dir / "人工复核.xlsx", results)
    write_details_md(output_dir / "批改详情.md", answer_key, results)

    # Class analysis is optional
    if not args.no_ai_analysis:
        try:
            ai_args = argparse.Namespace(
                api_key=args.api_key, base_url=args.base_url, model=args.model,
                backend=args.backend, api_timeout=args.api_timeout,
                api_max_retries=args.api_max_retries,
                render_dpi=args.render_dpi, max_render_pages=args.max_render_pages,
                render_timeout=args.render_timeout,
                chat_json_mode=not args.no_chat_json_mode, trust_env=args.trust_env,
                verbose=args.verbose,
            )
            ai = make_ai_backend_standalone(ai_args, output_dir)
            write_class_analysis_md(
                output_dir / "班级分析.md", ai, answer_key, results, False,
                args.analysis_max_students,
            )
        except Exception as e:
            print(f"  [SKIP] Class analysis failed (no AI available): {e}")

    print_output_summary(output_dir)
    return 0


def export_canvas_grades(
    canvas_roster: list[dict[str, Any]],
    submissions: list[dict[str, Any]],
    output_path: Path,
) -> int:
    """Export existing Canvas assignment grades to an Excel file.

    No AI grading is performed — this simply pulls whatever scores are
    already recorded in Canvas for the given assignment.
    """
    import html as _html

    sub_map: dict[str, dict[str, Any]] = {}
    for sub in submissions:
        uid = sub.get("canvas_user_id")
        if uid is not None:
            sub_map[str(uid)] = sub

    rows: list[list[Any]] = [["学号", "姓名", "Canvas ID", "分数", "等级", "提交时间", "状态"]]

    matched_ids: set[str] = set()
    for entry in canvas_roster:
        uid = entry.get("canvas_user_id")
        sid = str(entry.get("sis_user_id", ""))
        name = str(entry.get("name", ""))
        sub = sub_map.get(str(uid)) if uid is not None else None
        matched_ids.add(str(uid))

        score = ""
        grade = ""
        submitted_at = ""
        status = "未提交"
        if sub:
            if sub.get("score") is not None:
                score = sub["score"]
            grade = str(sub.get("grade", "") or "")
            raw_time = sub.get("submitted_at") or ""
            if raw_time:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                    submitted_at = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    submitted_at = raw_time[:16]
            if sub.get("excused"):
                status = "豁免"
            elif sub.get("missing"):
                status = "缺交"
            elif sub.get("late"):
                status = "迟交"
            elif sub.get("workflow_state") == "graded":
                status = "已评分"
            elif sub.get("workflow_state") == "submitted":
                status = "待评分"
        rows.append([sid, name, uid, score, grade, submitted_at, status])

    # Append submissions not in roster
    for sub in submissions:
        uid = str(sub.get("canvas_user_id", ""))
        if uid in matched_ids:
            continue
        if sub.get("workflow_state") == "unsubmitted":
            continue
        name = str(sub.get("user_name", ""))
        score = sub.get("score", "")
        grade = str(sub.get("grade", "") or "")
        raw_time = sub.get("submitted_at") or ""
        submitted_at = ""
        if raw_time:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                submitted_at = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                submitted_at = raw_time[:16]
        status = "已评分" if sub.get("workflow_state") == "graded" else "待评分"
        if sub.get("late"):
            status = "迟交"
        rows.append(["", name, uid, score, grade, submitted_at, status])

    # Sort by student_id (skip header)
    header = rows[0]
    data = rows[1:]
    data.sort(key=lambda r: (str(r[0]), str(r[1])))
    rows = [header] + data

    # --- Self-contained xlsx writer ---
    def _col_letter(index: int) -> str:
        letters = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        return letters

    def _cell_xml(value: Any, ref: str) -> str:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{ref}"><v>{value}</v></c>'
        text = _html.escape(str(value))
        return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'

    def _sheet_xml(sheet_rows: list[list[Any]]) -> str:
        row_xml = []
        for ri, row in enumerate(sheet_rows, start=1):
            cells = []
            for ci, val in enumerate(row, start=1):
                ref = f"{_col_letter(ci)}{ri}"
                cells.append(_cell_xml(val, ref))
            row_xml.append(f'<row r="{ri}">{"".join(cells)}</row>')
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(row_xml)}</sheetData></worksheet>'
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>")
        zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(rows))
        zf.writestr("xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="成绩" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>")
        zf.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>")

    graded = sum(1 for r in rows[1:] if r[6] in ("已评分", "迟交", "豁免"))
    print(f"Exported {len(rows) - 1} students ({graded} graded) to {output_path}")
    return 0


def integrated_main(args: argparse.Namespace) -> int:
    """Run the full Canvas → grade → Canvas pipeline."""
    run_started_at = time.time()

    # --- Setup output directory ---
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Regenerate Excel only (no Canvas needed) ---
    if args.regenerate_excel:
        return _regenerate_output_files(output_dir, args)

    work_root = Path(tempfile.mkdtemp(prefix="canvas_grader_", dir=str(output_dir)))

    try:
        # --- Initialize Canvas client ---
        canvas_trust_env = args.trust_env
        if canvas_trust_env is None:
            canvas_trust_env = True  # Default: honor system proxy for Canvas
        canvas = CanvasClient(args.canvas_url, args.canvas_token, trust_env=canvas_trust_env)
        course_id = args.canvas_course_id
        assignment_id = args.canvas_assignment_id

        # --- Fetch roster ---
        print("=" * 60)
        print("Fetching course roster from Canvas...")
        canvas_roster = canvas.get_course_roster(course_id)
        local_roster = read_roster(args.roster) if args.roster else None
        grading_roster = merge_canvas_and_local_roster(canvas_roster, local_roster)

        # --- Fetch submissions ---
        print("Fetching submissions from Canvas...")
        submissions = canvas.get_submissions(course_id, assignment_id)

        # --- Export grades only (no AI grading) ---
        if args.canvas_export_grades:
            export_path = output_dir / "Canvas成绩导出.xlsx"
            return export_canvas_grades(grading_roster, submissions, export_path)

        submitted = [s for s in submissions if s["workflow_state"] not in ("unsubmitted",)]
        if not submitted:
            print("No submitted assignments found. Exiting.")
            return 0

        # Sort by user_id for deterministic ordering
        submitted.sort(key=lambda s: s["canvas_user_id"] or 0)

        # --- Filter to ungraded only if requested ---
        if args.canvas_ungraded_only:
            before = len(submitted)
            submitted = [s for s in submitted if s.get("workflow_state") != "graded"]
            print(f"--canvas-ungraded-only: filtered {before} -> {len(submitted)} ungraded submissions")
            if not submitted:
                print("All submitted assignments are already graded. Nothing to do.")
                return 0

        # --- Apply student slice if requested ---
        if args.canvas_student_slice:
            slice_parts = args.canvas_student_slice.strip().split(":")
            if len(slice_parts) != 2:
                raise SystemExit(f"Invalid student-slice format: '{args.canvas_student_slice}'. Use Python slice notation like '0:45' or '45:'.")
            start = int(slice_parts[0]) if slice_parts[0] else 0
            stop = int(slice_parts[1]) if slice_parts[1] else len(submitted)
            total = len(submitted)
            submitted = submitted[start:stop]
            print(f"Student slice: {start}:{stop} → {len(submitted)} of {total} students selected")

        # --- Build submission lookup (needed for already-graded check) ---
        # Key by canvas_user_id for reliable lookup during upload
        submission_map: dict[str, dict[str, Any]] = {
            str(s["canvas_user_id"]): s for s in submitted
        }

        # --- Download PDFs ---
        pdf_dir = work_root / "canvas_pdfs"
        student_pdfs: list[Path] = []

        if args.canvas_upload_only:
            print("--canvas-upload-only set; skipping PDF download.")
        else:
            print(f"Downloading PDFs for {len(submitted)} submissions...")
            for sub in submitted:
                attachments = sub.get("attachments") or []
                if not attachments:
                    print(f"  [SKIP] {sub['user_name']} (user {sub['canvas_user_id']}): no attachments")
                    continue

                # Use the first PDF attachment (or the only one)
                pdf_attachments = [
                    a for a in attachments
                    if (a.get("content-type") or "").lower() == "application/pdf"
                       or Path(a.get("display_name") or a.get("filename") or "").suffix.lower() == ".pdf"
                ]
                if not pdf_attachments:
                    # Fall back to first attachment regardless of type
                    pdf_attachments = [attachments[0]]

                attachment = pdf_attachments[0]
                user_id = sub["canvas_user_id"]
                user_name = sub.get("user_name", f"user_{user_id}")
                roster_entry = next((r for r in grading_roster if r.get("canvas_user_id") == user_id), None)
                student_sid = roster_entry.get("student_id", "") if roster_entry else ""
                safe_name = re.sub(r"[^\w一-鿿\-]", "_", user_name)[:40]
                if student_sid:
                    preferred = f"{student_sid}_{safe_name}"
                else:
                    preferred = f"canvas_{user_id}_{safe_name}"

                local_path = canvas.download_attachment(attachment, pdf_dir, preferred)
                if local_path and local_path.stat().st_size > 0:
                    student_pdfs.append(local_path)
                    submission_map[local_path.name] = sub
                    status = ""
                    if sub.get("late"):
                        status += " [LATE]"
                    if sub.get("workflow_state") == "graded":
                        status += f" [already graded: {sub.get('score')}]"
                    print(f"  [OK] {user_name} -> {local_path.name}{status}")
                else:
                    print(f"  [FAIL] {sub['user_name']} (user {user_id}): download failed or empty file")

        if args.canvas_fetch_only:
            print(f"\nFetched {len(student_pdfs)} PDFs to {pdf_dir}")
            print("--canvas-fetch-only set; stopping here.")
            return 0

        if not args.canvas_upload_only and not student_pdfs:
            print("No PDFs downloaded. Exiting.")
            return 0

        # --- Upload-only: load existing results and skip AI entirely ---
        if args.canvas_upload_only:
            results_path = output_dir / "results.json"
            if not results_path.exists():
                raise SystemExit(f"results.json not found at {results_path}. Cannot use --canvas-upload-only.")
            results = sanitize_for_output(json.loads(results_path.read_text(encoding="utf-8")))
            answer_key_path = output_dir / "answer_key.json"
            answer_key = json.loads(answer_key_path.read_text(encoding="utf-8")) if answer_key_path.exists() else {}
            print(f"Loaded {len(results)} existing results from {results_path}")
            if results and "_score_decimals" not in results[0]:
                results[0]["_score_decimals"] = args.score_decimals
        else:
            # --- AI grading setup ---
            ai_args = argparse.Namespace(
                api_key=args.api_key,
                base_url=args.base_url,
                model=args.model,
                backend=args.backend,
                api_timeout=args.api_timeout,
                api_max_retries=args.api_max_retries,
                render_dpi=args.render_dpi,
                max_render_pages=args.max_render_pages,
                render_timeout=args.render_timeout,
                chat_json_mode=not args.no_chat_json_mode,
                trust_env=args.trust_env,
                verbose=args.verbose,
            )
            ai = make_ai_backend_standalone(ai_args, work_root)
            rate_limiter = ApiRateLimiter(args.requests_per_minute)
            if rate_limiter.enabled:
                print(f"AI request rate limit: {rate_limiter.limit} requests/minute")
            if args.answer_key_json:
                answer_key = json.loads(Path(args.answer_key_json).read_text(encoding="utf-8"))
                answer_key = apply_point_allocation(
                    answer_key, args.regular_points, args.bonus_points, args.point_mode, args.score_decimals
                )
                (output_dir / "answer_key.json").write_text(
                    json.dumps(answer_key, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            elif args.answer:
                answer_pdf = Path(args.answer).expanduser().resolve()
                answer_key = load_or_extract_answer_key(
                    ai=ai,
                    answer_pdf=answer_pdf,
                    output_dir=output_dir,
                    regular_points=args.regular_points,
                    bonus_points=args.bonus_points,
                    point_mode=args.point_mode,
                    score_decimals=args.score_decimals,
                    refresh_answer_key=args.refresh_answer_key,
                    rate_limiter=rate_limiter,
                )
            else:
                raise SystemExit("Provide --answer or --answer-key-json for the reference answer PDF.")

            # --- Grade ---
            extra_rules = build_extra_scoring_rules() if not args.no_ta_scoring else ""
            print("=" * 60)
            print(f"Grading {len(student_pdfs)} student PDFs...")
            if extra_rules:
                print("TA scoring rules: regular lenient (-0.1/minor), bonus strict (-0.5/wrong)")
            existing_results = load_existing_results(output_dir) if args.resume else {}
            if existing_results and args.resume:
                print(f"Found {len(existing_results)} reusable existing result(s).")
            results = run_grading_pipeline(
                ai=ai,
                student_pdfs=student_pdfs,
                answer_key=answer_key,
                roster=grading_roster,
                output_dir=output_dir,
                regular_points=args.regular_points,
                bonus_points=args.bonus_points,
                grading_mode=args.grading_mode,
                review_threshold=args.review_threshold,
                score_decimals=args.score_decimals,
                review_zero_scores=args.review_zero_scores,
                extra_scoring_rules=extra_rules,
                max_workers=args.max_workers,
                rate_limiter=rate_limiter,
                existing_results=existing_results,
                max_new_pdfs=args.max_pdfs,
            )

            # --- Second-pass AI review for flagged questions ---
            if not args.no_review_pass:
                print("=" * 60)
                print("Second-pass review for flagged questions...")
                review_flagged_questions(ai, results, answer_key, extra_rules, rate_limiter=rate_limiter)

            # --- Attach canvas_user_id to each result ---
            for result in results:
                canvas_uid = resolve_canvas_user_id(result, grading_roster)
                if canvas_uid is not None:
                    result["canvas_user_id"] = canvas_uid

            # --- Statistical outlier check ---
            if len(results) >= 3:
                print("=" * 60)
                print("Statistical outlier detection...")
                detect_score_outliers(results)

            # --- Write output files ---
            if results:
                results[0]["_score_decimals"] = args.score_decimals
            results = sanitize_for_output(results)
            (output_dir / "results.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            write_clean_grades(
                output_dir / "总成绩_三列表.xlsx", results, grading_roster, args.blank_review_scores
            )
            write_details_xlsx(output_dir / "批改明细.xlsx", results)
            write_review_xlsx(output_dir / "人工复核.xlsx", results)
            if args.output_profile == "full":
                write_details_md(output_dir / "批改详情.md", answer_key, results)
                write_class_analysis_md(
                    output_dir / "班级分析.md",
                    None if args.no_ai_analysis else ai,
                    answer_key,
                    results,
                    args.no_ai_analysis,
                    args.analysis_max_students,
                    rate_limiter=rate_limiter,
                )
            else:
                clean_compact_output_files(output_dir)
            write_rate_analysis_files(
                output_dir,
                rate_limiter,
                run_started_at,
                write_json=args.output_profile == "full",
            )
            write_submission_diagnostics(output_dir, results, write_json=args.output_profile == "full")

        # --- Upload grades to Canvas ---
        if args.canvas_skip_upload:
            print("\n--canvas-skip-upload set; skipping grade upload.")
            print_output_summary(output_dir)
            return 0

        print("=" * 60)
        if args.canvas_dry_run_upload:
            print("DRY RUN: previewing grades that would be uploaded...\n")

        upload_count = 0
        skip_count = 0
        error_count = 0

        for result in results:
            canvas_uid = resolve_canvas_user_id(result, grading_roster)
            if canvas_uid is None:
                print(f"  [SKIP] {result.get('name', '?')} ({result.get('student_id', '?')}): "
                      f"could not resolve Canvas user_id")
                skip_count += 1
                continue

            score = result.get("total_score", 0)

            # Check if submission was previously graded
            sub_info = submission_map.get(str(canvas_uid))
            if sub_info and sub_info.get("workflow_state") == "graded" and not args.canvas_overwrite_grades:
                print(f"  [SKIP] {result.get('name', '')}: already graded ({sub_info.get('score')}) "
                      f"-- use --canvas-overwrite-grades to overwrite")
                skip_count += 1
                continue

            comment = format_feedback_comment(result, answer_key)

            if args.canvas_dry_run_upload:
                print(f"  [DRY RUN] user={canvas_uid} | {result.get('name', '')} "
                      f"({result.get('student_id', '')}) | score={score}")
                upload_count += 1
                continue

            try:
                canvas.put_grade(course_id, assignment_id, canvas_uid, score, comment)
                print(f"  [OK] {result.get('name', '')} ({result.get('student_id', '')}): "
                      f"{score}/{result.get('max_score', 0)}")
                upload_count += 1
            except Exception as exc:
                print(f"  [FAIL] {result.get('name', '')} ({result.get('student_id', '')}): {exc}")
                error_count += 1

        print(f"\nUpload summary: {upload_count} uploaded, {skip_count} skipped, {error_count} errors")
        return 0

    finally:
        canvas.close()
        if args.keep_workdir:
            print(f"Kept workdir: {work_root}")
        else:
            shutil.rmtree(work_root, ignore_errors=True)


def print_output_summary(output_dir: Path) -> None:
    """Print paths to generated output files."""
    print("\nDone.")
    for name in ["总成绩_三列表.xlsx", "批改明细.xlsx", "人工复核.xlsx", "批改详情.md", "班级分析.md"]:
        p = output_dir / name
        if p.exists():
            print(f"  {name}: {p}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def make_ai_backend_standalone(args: argparse.Namespace, work_root: Path) -> AIBackend:
    """Create an AIBackend from an argparse Namespace (standalone version for canvas_integration)."""
    if not args.api_key:
        raise SystemExit(
            "Missing API key. Set AI_GRADER_API_KEY or OPENAI_API_KEY, or pass --api-key.\n"
            'PowerShell example: $env:AI_GRADER_API_KEY="your_api_key"'
        )
    client_kwargs: dict[str, Any] = {"api_key": args.api_key}
    if args.base_url:
        client_kwargs["base_url"] = args.base_url
    client_kwargs["max_retries"] = int(getattr(args, "api_max_retries", 0))

    trust_env = args.trust_env
    # Honor env var AI_GRADER_TRUST_ENV if trust_env not explicitly set
    if trust_env is None:
        env_val = os.getenv("AI_GRADER_TRUST_ENV")
        if env_val is not None and env_val.strip() != "":
            trust_env = env_val.strip().lower() in {"1", "true", "yes", "y", "on"}
        else:
            trust_env = False if args.base_url else True

    client_kwargs["http_client"] = httpx.Client(timeout=float(args.api_timeout), trust_env=trust_env)
    from openai import OpenAI
    client = OpenAI(**client_kwargs)
    return AIBackend(
        client=client,
        model=args.model,
        backend=args.backend,
        work_root=work_root,
        render_dpi=args.render_dpi,
        max_render_pages=args.max_render_pages,
        render_timeout=args.render_timeout,
        chat_json_mode=args.chat_json_mode,
        verbose=getattr(args, "verbose", False),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Canvas LMS → AI grading → Canvas: fully automated homework grading pipeline."
    )

    # ---- Canvas connection ----
    canvas_group = parser.add_argument_group("Canvas connection")
    canvas_group.add_argument("--canvas-token", default=os.getenv("CANVAS_API_TOKEN"),
                              help="Canvas API access token. Defaults to CANVAS_API_TOKEN env var.")
    canvas_group.add_argument("--canvas-url", default=os.getenv("CANVAS_BASE_URL", "https://oc.sjtu.edu.cn/api/v1"),
                              help="Canvas API base URL. Defaults to CANVAS_BASE_URL or https://oc.sjtu.edu.cn/api/v1")
    canvas_group.add_argument("--canvas-course-id", default=os.getenv("CANVAS_COURSE_ID"),
                              help="Canvas course ID. Defaults to CANVAS_COURSE_ID env var.")
    canvas_group.add_argument("--canvas-assignment-id", default=os.getenv("CANVAS_ASSIGNMENT_ID"),
                              help="Canvas assignment ID. Defaults to CANVAS_ASSIGNMENT_ID env var.")

    # ---- Config file ----
    parser.add_argument("--config", help="JSON config file with any of the above/below parameters.")

    # ---- Canvas workflow control ----
    flow_group = parser.add_argument_group("Canvas workflow control")
    flow_group.add_argument("--canvas-fetch-only", action="store_true",
                            help="Only download submissions, don't grade or upload.")
    flow_group.add_argument("--canvas-upload-only", action="store_true",
                            help="Skip fetching & grading; upload from existing results.json.")
    flow_group.add_argument("--canvas-dry-run-upload", action="store_true",
                            help="Preview uploads without actually posting grades.")
    flow_group.add_argument("--canvas-skip-upload", action="store_true",
                            help="Grade and generate reports, but skip Canvas upload.")
    flow_group.add_argument("--canvas-overwrite-grades", action="store_true",
                            help="Overwrite grades for students who were already graded in Canvas.")
    flow_group.add_argument("--canvas-ungraded-only", action="store_true",
                            help="Only process submissions that have not been graded yet. "
                                 "Skips already-graded students before downloading and grading.")
    flow_group.add_argument("--canvas-student-slice",
                            help="Python slice notation to select a subset of students, e.g. '0:45' for first 45, '45:90' for second 45, '45:' from 45 to end.")
    flow_group.add_argument("--no-review-pass", action="store_true",
                            help="Skip the second-pass AI review for flagged questions.")
    flow_group.add_argument("--no-ta-scoring", action="store_true",
                            help="Disable TA differentiated scoring (regular lenient -0.1/minor, bonus strict -0.5/wrong).")
    flow_group.add_argument("--regenerate-excel", action="store_true",
                            help="Regenerate all Excel/MD output files from existing results.json (no Canvas connection needed).")
    flow_group.add_argument("--canvas-export-grades", action="store_true",
                            help="Pull existing grades from Canvas and export to Excel. No AI grading performed.")

    # ---- Answer & input ----
    input_group = parser.add_argument_group("Answer and input")
    input_group.add_argument("--answer", help="Reference-answer PDF path.")
    input_group.add_argument("--answer-key-json", help="Use a pre-extracted answer key JSON (skip AI extraction).")
    input_group.add_argument("--refresh-answer-key", action="store_true",
                             help="Ignore cached answer_key.json and re-extract the answer key with AI.")
    input_group.add_argument("--roster", help="Optional local .xlsx roster with student_id (学号) and name columns. Merges with Canvas data to fill missing student IDs.")

    # ---- AI provider ----
    ai_group = parser.add_argument_group("AI provider")
    ai_group.add_argument("--model", default=os.getenv("AI_GRADER_MODEL", "gpt-5.1"))
    ai_group.add_argument("--backend", choices=["responses", "chat-vision"],
                          default=os.getenv("AI_GRADER_BACKEND", "responses"))
    ai_group.add_argument("--api-key", default=os.getenv("AI_GRADER_API_KEY") or os.getenv("OPENAI_API_KEY"))
    ai_group.add_argument("--base-url", default=os.getenv("AI_GRADER_BASE_URL") or os.getenv("OPENAI_BASE_URL"))
    ai_group.add_argument("--api-timeout", type=float, default=float(os.getenv("AI_GRADER_API_TIMEOUT", "120")))
    ai_group.add_argument("--api-max-retries", type=int, default=int(os.getenv("AI_GRADER_API_MAX_RETRIES", "0")),
                          help="OpenAI SDK automatic retry count. Default 0 avoids long retry stalls.")

    # ---- Scoring ----
    score_group = parser.add_argument_group("Scoring")
    score_group.add_argument("--regular-points", type=float, default=10.0)
    score_group.add_argument("--bonus-points", type=float, default=2.0)
    score_group.add_argument("--point-mode", choices=["total", "per-question"], default="total")
    score_group.add_argument("--grading-mode", choices=["standard", "lenient", "strict"],
                             default=os.getenv("AI_GRADER_GRADING_MODE", "standard"))

    # ---- Grading options ----
    grad_group = parser.add_argument_group("Grading options")
    grad_group.add_argument("--review-threshold", type=float, default=0.65)
    grad_group.add_argument("--score-decimals", type=int, default=2)
    grad_group.add_argument("--no-review-zero-scores", action="store_false", dest="review_zero_scores",
                            default=True)
    grad_group.add_argument("--blank-review-scores", action="store_true")

    # ---- Chat-vision options ----
    vis_group = parser.add_argument_group("Chat-vision rendering")
    vis_group.add_argument("--render-dpi", type=int, default=160)
    vis_group.add_argument("--max-render-pages", type=int, default=12)
    vis_group.add_argument("--render-timeout", type=float, default=float(os.getenv("AI_GRADER_RENDER_TIMEOUT", "120")))
    vis_group.add_argument("--no-chat-json-mode", action="store_true")

    # ---- Analysis & output ----
    out_group = parser.add_argument_group("Output")
    out_group.add_argument("--output-dir", default="output")
    out_group.add_argument("--no-ai-analysis", action="store_true")
    out_group.add_argument("--analysis-max-students", type=int, default=120)
    out_group.add_argument("--max-pdfs", type=int, help="Limit number of PDFs to process (for testing).")
    out_group.add_argument("--max-workers", type=int, default=1,
                           help="Maximum number of student PDFs to grade concurrently. Default 1 keeps serial behavior.")
    out_group.add_argument("--requests-per-minute", type=float, default=0.0,
                           help="Global AI request limit per minute for grading/review/analysis. 0 means unlimited.")
    out_group.add_argument("--output-profile", choices=["compact", "full"],
                           default=os.getenv("AI_GRADER_OUTPUT_PROFILE", "compact"),
                           help="compact writes essential outputs; full writes all debug/report files.")
    out_group.add_argument("--resume", action="store_true", dest="resume", default=True,
                           help="Reuse existing successful results from results.json/partial_results.json. Enabled by default.")
    out_group.add_argument("--no-resume", action="store_false", dest="resume",
                           help="Ignore existing results and grade every submission again.")

    # ---- Misc ----
    misc_group = parser.add_argument_group("Miscellaneous")
    misc_group.add_argument("--keep-workdir", action="store_true")
    misc_group.add_argument("--verbose", action="store_true", help="Show per-file rendering/API diagnostic logs.")
    misc_group.add_argument("--trust-env", action="store_true", dest="trust_env", default=None)
    misc_group.add_argument("--no-trust-env", action="store_false", dest="trust_env")

    args = parser.parse_args(argv)

    # --- Load config file if specified ---
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        if not config_path.exists():
            raise SystemExit(f"Config file not found: {config_path}")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        # Apply config values as defaults (CLI args take precedence)
        for key, value in config.items():
            attr = key.replace("-", "_")
            if hasattr(args, attr):
                current = getattr(args, attr)
                # Only fill from config if CLI/default didn't already set a non-default value
                # For string values, empty string or None means not set
                if isinstance(value, str) and (current is None or current == "" or current == parser.get_default(attr)):
                    setattr(args, attr, value)
                elif isinstance(value, bool) and current == parser.get_default(attr):
                    setattr(args, attr, value)
                elif isinstance(value, (int, float)) and current == parser.get_default(attr):
                    setattr(args, attr, value)

    # --- Validate required params ---
    if not args.canvas_token:
        raise SystemExit(
            "Missing Canvas API token. Set CANVAS_API_TOKEN env var or pass --canvas-token.\n"
            "Generate a token at: https://oc.sjtu.edu.cn/profile/settings"
        )
    if not args.canvas_course_id:
        raise SystemExit("Missing --canvas-course-id or CANVAS_COURSE_ID env var.")
    if not args.canvas_assignment_id:
        raise SystemExit("Missing --canvas-assignment-id or CANVAS_ASSIGNMENT_ID env var.")
    if not args.canvas_upload_only and not args.canvas_export_grades and not args.answer and not args.answer_key_json:
        raise SystemExit("Provide --answer or --answer-key-json for the reference answer PDF.")
    if args.max_workers < 1:
        raise SystemExit("--max-workers must be >= 1.")
    if args.requests_per_minute < 0:
        raise SystemExit("--requests-per-minute must be >= 0.")
    if args.api_timeout <= 0:
        raise SystemExit("--api-timeout must be > 0.")
    if args.api_max_retries < 0:
        raise SystemExit("--api-max-retries must be >= 0.")
    if args.render_timeout <= 0:
        raise SystemExit("--render-timeout must be > 0.")

    return args


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(integrated_main(parse_args()))
