"""从固定测试数据生成可追溯的老板早报 V0。"""

from __future__ import annotations

import csv
import json
import re
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from testagent_dev_agent.paths import REPOSITORY_ROOT


OWNER_BRIEFING_TOOL_NAME = "generate_owner_daily_briefing"
OWNER_BRIEFING_SCENARIO = "owner_daily_briefing"
OWNER_BRIEFING_DATA_DIR = REPOSITORY_ROOT / "fixtures" / "owner_briefing_v0"
OWNER_BRIEFING_OUTPUT_DIR = REPOSITORY_ROOT / "artifacts" / "owner_briefing"
CHINA_TIMEZONE = timezone(timedelta(hours=8))
_YUAN_AMOUNT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?) 元")
_ANOMALY_LABELS = {
    "attendance_absent": "考勤",
    "attendance_late": "考勤",
    "attendance_location": "考勤",
    "attendance_missing_clock": "考勤",
    "customer_followup_overdue": "客户跟进",
    "project_start_not_ready": "开工准备",
    "project_progress_delay": "项目进度",
    "receivable_overdue": "回款",
    "complaint_overdue": "投诉处理",
}

OWNER_DAILY_BRIEFING_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": OWNER_BRIEFING_TOOL_NAME,
        "description": (
            "为柒昱美学设计公司生成指定日期的老板经营早报。"
            "读取固定测试数据，按确定性规则汇总考勤、客户、项目、应收和投诉异常，"
            "返回来源记录、责任人、建议动作和可下载产物。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "report_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD；测试范围为 2026-08-07 至 2026-08-13。",
                }
            },
            "required": ["report_date"],
            "additionalProperties": False,
        },
    },
}


def generate_owner_daily_briefing(
    report_date: str,
    *,
    data_dir: Path = OWNER_BRIEFING_DATA_DIR,
    output_dir: Path = OWNER_BRIEFING_OUTPUT_DIR,
) -> dict[str, Any]:
    try:
        business_date = date.fromisoformat(report_date)
    except (TypeError, ValueError):
        return _error("INVALID_REPORT_DATE", "report_date 必须是 YYYY-MM-DD 格式。")
    try:
        rules = json.loads((data_dir / "company_rules.json").read_text("utf-8"))
        sources = {
            "attendance.csv": _rows(data_dir / "attendance.csv", "date", report_date),
            "customer_followups.csv": _rows(
                data_dir / "customer_followups.csv", "snapshot_date", report_date
            ),
            "projects.csv": _rows(data_dir / "projects.csv", "snapshot_date", report_date),
            "finance_cases.csv": _rows(
                data_dir / "finance_cases.csv", "snapshot_date", report_date
            ),
        }
        if not any(sources.values()):
            return _error(
                "REPORT_DATE_NOT_FOUND",
                f"固定测试数据中没有 {report_date} 的记录。",
                retryable=True,
            )
        briefing = _build(business_date, rules, sources)
        artifacts = _write_artifacts(briefing, output_dir)
    except (OSError, UnicodeError, csv.Error, json.JSONDecodeError, ValueError, KeyError) as exc:
        return _error("OWNER_BRIEFING_DATA_ERROR", str(exc))
    return {
        "success": True,
        "workflow": "owner_daily_briefing_v0",
        "company_name": briefing["company_name"],
        "report_date": report_date,
        "briefing": briefing,
        "artifacts": artifacts,
        "message": f"{report_date} 老板早报已生成，共发现 {len(briefing['anomalies'])} 项异常。",
        "assistant_summary": _assistant_summary(briefing),
    }


def attach_owner_briefing_presentation(registry: Any, result: dict[str, Any]) -> dict[str, Any]:
    response = deepcopy(result)
    if result.get("success") is True and isinstance(result.get("briefing"), dict):
        response["presentation"] = registry.register(
            OWNER_BRIEFING_SCENARIO, result["briefing"]
        )
    return response


def compact_owner_briefing_result(
    result: dict[str, Any],
    *,
    ui_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保留完整业务条目，同时移除只供可信 UI 使用的大字段。"""

    if result.get("success") is not True or not isinstance(
        result.get("briefing"), dict
    ):
        return deepcopy(result)
    briefing = result["briefing"]
    anomaly_fields = (
        "anomaly_key",
        "issue_key",
        "anomaly_type",
        "severity",
        "category",
        "title",
        "trigger",
        "recommendation",
        "owner",
        "evidence_ref",
        "status",
        "status_label",
        "assignee",
        "note",
        "due_at_local",
        "reminder_count",
        "escalation_pending_label",
    )
    compact_briefing = {
        key: deepcopy(briefing[key])
        for key in (
            "company_name",
            "report_date",
            "summary",
            "live_state_notice",
            "metrics",
        )
        if key in briefing
    }
    compact_briefing["anomalies"] = [
        {key: deepcopy(item[key]) for key in anomaly_fields if key in item}
        for item in briefing.get("anomalies", [])
        if isinstance(item, dict)
    ]
    if isinstance(briefing.get("previous_day_review"), dict):
        compact_briefing["previous_day_review"] = deepcopy(
            briefing["previous_day_review"]
        )

    response = {
        key: deepcopy(result[key])
        for key in (
            "success",
            "workflow",
            "company_name",
            "report_date",
            "artifacts",
            "message",
            "assistant_summary",
        )
        if key in result
    }
    response["compact_result_complete"] = True
    response["briefing"] = compact_briefing
    if ui_result is not None:
        response["ui_presented"] = ui_result.get("success") is True
        response["ui"] = deepcopy(ui_result)
    return response


def attach_owner_next_day_review(
    result: dict[str, Any], review: dict[str, Any]
) -> dict[str, Any]:
    """把前一日闭环复盘附加到早报，并刷新同一份可下载产物。"""

    response = deepcopy(result)
    if response.get("success") is not True or not isinstance(
        response.get("briefing"), dict
    ):
        return response
    response["briefing"]["previous_day_review"] = deepcopy(review)
    artifacts = response.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        raw_path = artifacts[0].get("path")
        if isinstance(raw_path, str) and raw_path:
            path = Path(raw_path)
            if not path.is_absolute():
                path = REPOSITORY_ROOT / path
            response["artifacts"] = _write_artifacts(
                response["briefing"], path.parent.parent
            )
    return response


def _build(
    business_date: date,
    rules: dict[str, Any],
    sources: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    report_date = business_date.isoformat()
    company = str(rules["company_name"])
    attendance = sources["attendance.csv"]
    customers = sources["customer_followups.csv"]
    projects = sources["projects.csv"]
    finance = sources["finance_cases.csv"]
    anomalies: list[dict[str, Any]] = []

    attendance_rules = rules["attendance"]
    start = datetime.combine(
        business_date, time.fromisoformat(attendance_rules["work_start"])
    ) + timedelta(minutes=int(attendance_rules["late_grace_minutes"]))
    for row in attendance:
        if row["attendance_status"] == "请假" and _bool(row["leave_approved"]):
            continue
        if row["attendance_status"] == "缺勤":
            _add(
                anomalies, row, "attendance_absent", "critical", "attendance",
                f"{row['employee_name']} 未请假且无出勤记录",
                "未批准请假但考勤状态为缺勤",
                "请直属负责人立即确认人员状态和当日工作交接。",
                row["manager"], "attendance.csv",
                ("employee_name", "clock_in", "clock_out", "attendance_status", "location_status"),
            )
            continue
        if not row["clock_in"] or not row["clock_out"]:
            _add(
                anomalies, row, "attendance_missing_clock", "medium", "attendance",
                f"{row['employee_name']} 存在缺卡记录", "上班或下班打卡时间缺失",
                "请直属负责人当天完成缺卡原因核验。", row["manager"],
                "attendance.csv", ("employee_name", "clock_in", "clock_out", "attendance_status", "location_status"),
            )
            continue
        if time.fromisoformat(row["clock_in"]) > start.time():
            _add(
                anomalies, row, "attendance_late", "medium", "attendance",
                f"{row['employee_name']} 于 {row['clock_in']} 打卡",
                f"超过 {attendance_rules['work_start']} 后 {attendance_rules['late_grace_minutes']} 分钟宽限时间",
                "请直属负责人确认迟到原因和是否影响当日安排。", row["manager"],
                "attendance.csv", ("employee_name", "clock_in", "clock_out", "attendance_status", "location_status"),
            )
        if row["location_status"] != attendance_rules["allowed_location_status"]:
            _add(
                anomalies, row, "attendance_location", "high", "attendance",
                f"{row['employee_name']} 打卡位置异常",
                f"location_status 不是 {attendance_rules['allowed_location_status']}",
                "请直属负责人核验打卡位置和外出安排。", row["manager"],
                "attendance.csv", ("employee_name", "clock_in", "clock_out", "attendance_status", "location_status"),
            )

    customer_rules = rules["customer"]
    high_intent = set(customer_rules["high_intent_stages"])
    report_time = datetime.combine(business_date, time(9, 30), CHINA_TIMEZONE)
    for row in customers:
        if row["status"] != "active":
            continue
        hours = (report_time - datetime.fromisoformat(row["last_contact_at"])).total_seconds() / 3600
        important = row["stage"] in high_intent
        threshold = float(
            customer_rules[
                "high_intent_overdue_hours" if important else "followup_overdue_hours"
            ]
        )
        if hours > threshold:
            _add(
                anomalies, row, "customer_followup_overdue",
                "high" if important else "medium", "customer",
                f"{row['customer_name']} 已 {int(hours)} 小时未跟进",
                f"{row['stage']} 阶段跟进间隔超过 {int(threshold)} 小时",
                "请客户负责人今天更新沟通结果和下一步计划。", row["owner"],
                "customer_followups.csv", ("customer_name", "stage", "last_contact_at", "expected_amount"),
            )

    gap_limit = float(rules["project"]["progress_gap_percent"])
    for row in projects:
        fields = (
            "project_name", "stage", "planned_start_date", "expected_progress",
            "actual_progress", "materials_ready", "site_ready", "owner_confirmed",
        )
        missing = [
            label
            for label, field in (("材料", "materials_ready"), ("现场", "site_ready"), ("责任人", "owner_confirmed"))
            if not _bool(row[field])
        ]
        if row["stage"] == "待开工" and row["planned_start_date"] == report_date and missing:
            _add(
                anomalies, row, "project_start_not_ready", "high", "project",
                f"{row['project_name']} 今日开工准备不完整", f"未确认：{'、'.join(missing)}",
                "请项目负责人在开工前补齐并回报准备状态。", row["owner"],
                "projects.csv", fields,
            )
        gap = float(row["expected_progress"] or 0) - float(row["actual_progress"] or 0)
        if row["stage"] == "施工中" and gap >= gap_limit:
            _add(
                anomalies, row, "project_progress_delay", "high", "project",
                f"{row['project_name']} 进度落后 {gap:g} 个百分点",
                f"计划进度与实际进度差值达到 {gap_limit:g} 个百分点",
                "请项目负责人说明原因、补救动作和预计恢复时间。", row["owner"],
                "projects.csv", fields,
            )

    finance_rules = rules["finance"]
    for row in finance:
        fields = (
            "case_type", "project_id", "customer_name", "amount", "due_date",
            "opened_at", "status", "severity",
        )
        if row["case_type"] == "receivable" and row["status"] == "unpaid":
            due = date.fromisoformat(row["due_date"])
            if due < business_date:
                days = (business_date - due).days
                amount = _amount(row["amount"])
                critical = days >= int(finance_rules["receivable_red_days"]) or amount >= float(
                    finance_rules["receivable_red_amount"]
                )
                _add(
                    anomalies, row, "receivable_overdue", "critical" if critical else "high", "finance",
                    f"{row['customer_name']} 应收款 {amount:g} 元逾期 {days} 天",
                    "应收款已超过约定到期日",
                    "请项目负责人确认回款计划；任何停工或财务动作仍需人工审批。",
                    row["owner"], "finance_cases.csv", fields,
                )
        elif row["case_type"] == "complaint" and row["status"] == "open":
            hours = (report_time - datetime.fromisoformat(row["opened_at"])).total_seconds() / 3600
            threshold = float(finance_rules["complaint_escalation_hours"])
            if hours > threshold:
                _add(
                    anomalies, row, "complaint_overdue",
                    "critical" if row["severity"] == "high" else "high", "finance",
                    f"{row['customer_name']} 投诉已 {int(hours)} 小时未关闭",
                    f"开放投诉持续时间超过 {int(threshold)} 小时",
                    "请负责人立即同步处理进展、客户反馈和预计关闭时间。",
                    row["owner"], "finance_cases.csv", fields,
                )

    order = {"critical": 0, "high": 1, "medium": 2}
    anomalies.sort(key=lambda item: (order[item["severity"]], item["category"], item["anomaly_key"]))
    for item in anomalies:
        item["evidence_ref"] = f"{item['evidence']['source_file']} / {item['evidence']['record_id']}"
        item["task_id"] = str(uuid5(NAMESPACE_URL, f"{company}:{report_date}:{item['anomaly_key']}"))
        item.update(
            {
                "status": "detected",
                "status_label": "待处理",
                "assignee": "",
                "note": "",
                "assigned_at": "",
                "due_at": "",
                "due_at_local": "",
                "next_reminder_at": "",
                "last_reminded_at": "",
                "reminder_count": 0,
                "escalation_pending": False,
                "escalation_pending_label": "否",
            }
        )

    severity_counts = {level: sum(a["severity"] == level for a in anomalies) for level in order}
    overdue_amount = sum(
        _amount(row["amount"])
        for row in finance
        if row["case_type"] == "receivable"
        and row["status"] == "unpaid"
        and date.fromisoformat(row["due_date"]) < business_date
    )
    metrics = {
        "attendance_records": len(attendance),
        "active_customers": sum(row["status"] == "active" for row in customers),
        "today_appointments": sum(row["appointment_date"] == report_date for row in customers),
        "project_records": len(projects),
        "today_starts": sum(row["planned_start_date"] == report_date for row in projects),
        "overdue_receivable_amount": overdue_amount,
        "open_complaints": sum(row["case_type"] == "complaint" and row["status"] == "open" for row in finance),
        "anomaly_count": len(anomalies),
        **{f"{level}_count": count for level, count in severity_counts.items()},
    }
    return {
        "schema_version": "1.0",
        "company_name": company,
        "report_date": report_date,
        "generated_at": f"{report_date}T09:30:00+08:00",
        "summary": (
            f"共发现 {len(anomalies)} 项异常：紧急 {severity_counts['critical']} 项、"
            f"重要 {severity_counts['high']} 项、一般 {severity_counts['medium']} 项。"
        ),
        "live_state_notice": (
            "卡片数据会按当前任务库实时同步；Agent 对话正文保留生成时快照，"
            "请以卡片状态为准。"
        ),
        "metrics": metrics,
        "anomalies": anomalies,
        "source_files": list(sources),
    }


def _add(
    target: list[dict[str, Any]], row: dict[str, str], anomaly_type: str,
    severity: str, category: str, title: str, trigger: str, recommendation: str,
    owner: str, source_file: str, evidence_fields: tuple[str, ...],
) -> None:
    record_id = row["record_id"]
    target.append(
        {
            "anomaly_key": f"{anomaly_type}:{record_id}",
            "issue_key": f"{anomaly_type}:{_issue_entity_id(source_file, row)}",
            "anomaly_type": anomaly_type,
            "severity": severity,
            "title": title,
            "trigger": trigger,
            "recommendation": recommendation,
            "category": category,
            "owner": owner or "未指定",
            "evidence": {
                "source_file": source_file,
                "record_id": record_id,
                "observed_fields": {field: row[field] for field in evidence_fields},
            },
        }
    )


def _assistant_summary(briefing: dict[str, Any]) -> str:
    anomalies = [
        item for item in briefing.get("anomalies", []) if isinstance(item, dict)
    ]
    if not anomalies:
        return "早报已整理好，今天没有发现需要处理的异常。"

    priorities = anomalies[:3]
    lines = [f"早报已整理好。今天建议先处理 {len(priorities)} 件事："]
    needs_approval = False
    for item in priorities:
        recommendation = str(item.get("recommendation", "")).strip().rstrip("。")
        clauses = [part.strip() for part in recommendation.split("；") if part.strip()]
        needs_approval = needs_approval or any("人工审批" in part for part in clauses)
        label = _ANOMALY_LABELS.get(str(item.get("anomaly_type", "")), "重点事项")
        title = _assistant_title(item)
        action = _owner_action(str(item.get("owner", "")).strip(), clauses[0] if clauses else "")
        lines.append(f"- **{label}**：{title}。{action}")

    lines.append("")
    if needs_approval:
        lines.append("**需要你确认**：如需采取停工或财务处理，仍需你审批。")
    remaining = len(anomalies) - len(priorities)
    if remaining:
        lines.append(f"其余 {remaining} 项异常和完整证据已整理在下方。")
    else:
        lines.append("完整证据已整理在下方。")
    return "\n".join(lines)


def _assistant_title(item: dict[str, Any]) -> str:
    title = _format_yuan_amounts(str(item.get("title", "")).strip())
    separator = {
        "attendance_absent": (" 未请假", "未请假"),
        "attendance_late": (" 于", "于"),
        "attendance_location": (" 打卡", "打卡"),
        "attendance_missing_clock": (" 存在", "存在"),
        "customer_followup_overdue": (" 已", "已"),
        "project_start_not_ready": (" 今日", "今日"),
        "project_progress_delay": (" 进度", "进度"),
        "receivable_overdue": (" 应收款", "的应收款"),
        "complaint_overdue": (" 投诉", "的投诉"),
    }.get(str(item.get("anomaly_type", "")))
    return title.replace(*separator, 1) if separator else title


def _format_yuan_amounts(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        amount = float(match.group(1))
        if amount >= 10_000 and amount % 10_000 == 0:
            return f"{amount / 10_000:g} 万元"
        return match.group(0)

    return _YUAN_AMOUNT.sub(replace, text)


def _owner_action(owner: str, recommendation: str) -> str:
    action = recommendation.removeprefix("请")
    for role in ("项目负责人", "客户负责人", "直属负责人", "负责人"):
        if action.startswith(role):
            action = action.removeprefix(role)
            break
    actor = owner or "相关负责人"
    if action.startswith("今天"):
        return f"{actor}今天需要{action.removeprefix('今天')}。"
    if action.startswith("立即"):
        return f"{actor}需要立即{action.removeprefix('立即')}。"
    return f"{actor}需要{action}。" if action else f"由{actor}跟进。"


def _rows(path: Path, date_field: str, report_date: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or {"record_id", date_field} - set(reader.fieldnames):
            raise ValueError(f"{path.name} 缺少 record_id 或 {date_field}。")
        rows = [dict(row) for row in reader if row[date_field] == report_date]
    ids = [row["record_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path.name} 的 record_id 重复。")
    return rows


def _issue_entity_id(source_file: str, row: dict[str, str]) -> str:
    field = {
        "attendance.csv": "employee_id",
        "customer_followups.csv": "customer_id",
        "projects.csv": "project_id",
        "finance_cases.csv": "project_id",
    }.get(source_file)
    if field is None:
        return row["record_id"]
    return row.get(field) or row["record_id"]


def _write_artifacts(briefing: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    target = output_dir / briefing["report_date"]
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "owner-briefing.json"
    markdown_path = target / "owner-briefing.md"
    json_path.write_text(json.dumps(briefing, ensure_ascii=False, indent=2) + "\n", "utf-8")
    markdown_path.write_text(_markdown(briefing), "utf-8")
    return [_artifact(json_path, "application/json"), _artifact(markdown_path, "text/markdown")]


def _markdown(briefing: dict[str, Any]) -> str:
    metrics = briefing["metrics"]
    lines = [
        f"# {briefing['company_name']}老板早报", "", f"日期：{briefing['report_date']}", "",
        "## 今日结论", "", f"- {briefing['summary']}", "", "## 经营概览", "",
        f"- 考勤记录：{metrics['attendance_records']} 条",
        f"- 活跃客户：{metrics['active_customers']} 个；今日到店：{metrics['today_appointments']} 个",
        f"- 项目记录：{metrics['project_records']} 个；今日开工：{metrics['today_starts']} 个",
        f"- 逾期应收：{metrics['overdue_receivable_amount']:g} 元；开放投诉：{metrics['open_complaints']} 个",
        "", "## 需要处理的异常", "",
    ]
    for index, item in enumerate(briefing["anomalies"], 1):
        lines += [
            f"### {index}. [{item['severity']}] {item['title']}", "",
            f"- 责任人：{item['owner']}", f"- 触发规则：{item['trigger']}",
            f"- 证据：{item['evidence_ref']}", f"- 建议动作：{item['recommendation']}", "",
        ]
    review = briefing.get("previous_day_review")
    if isinstance(review, dict):
        metrics = review.get("metrics", {})
        lines += [
            "## 昨日闭环复盘", "",
            f"- 昨日新增：{metrics.get('new_count', 0)} 项",
            f"- 已完成：{metrics.get('completed_count', 0)} 项",
            f"- 未闭环：{metrics.get('unresolved_count', 0)} 项",
            f"- 已升级：{metrics.get('escalated_count', 0)} 项",
            f"- 今日重复出现：{metrics.get('recurring_count', 0)} 项", "",
        ]
        for item in review.get("recurring_issues", []):
            lines.append(
                f"- 重复问题：{item['title']}（昨日状态："
                f"{item.get('previous_status_label', item['previous_status'])}）"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _artifact(path: Path, media_type: str) -> dict[str, Any]:
    resolved = path.resolve()
    root = REPOSITORY_ROOT.resolve()
    stored = resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else str(resolved)
    return {"kind": "owner_briefing", "path": stored, "media_type": media_type, "size_bytes": resolved.stat().st_size}


def _bool(value: str) -> bool:
    if value in {"true", "True", "1"}:
        return True
    if value in {"false", "False", "0", ""}:
        return False
    raise ValueError(f"布尔字段值无效：{value}")


def _amount(value: str) -> float:
    amount = float(value)
    if amount < 0:
        raise ValueError("金额不能是负数。")
    return amount


def _error(error_type: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {"success": False, "error_type": error_type, "message": message, "retryable": retryable}


__all__ = [
    "OWNER_BRIEFING_SCENARIO", "OWNER_BRIEFING_TOOL_NAME", "OWNER_DAILY_BRIEFING_TOOL",
    "attach_owner_briefing_presentation", "attach_owner_next_day_review",
    "compact_owner_briefing_result", "generate_owner_daily_briefing",
]
