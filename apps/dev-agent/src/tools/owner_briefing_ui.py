"""老板早报 V0 的受控 Generation UI。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4


_ACTIONABLE_STATUSES = {"detected", "assigned"}
_SEVERITY_LABELS = {
    "critical": "紧急",
    "high": "重要",
    "medium": "一般",
}


def build_owner_briefing_ui_artifact(
    title: str, data: dict[str, Any], contract: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 80:
        raise ValueError("title 必须是 1-80 个字符。")
    required = {"company_name", "report_date", "summary", "metrics", "anomalies"}
    if not isinstance(data, dict) or required - set(data):
        raise ValueError("老板早报 UI 数据不完整。")
    artifact_data = deepcopy(data)
    anomalies = artifact_data["anomalies"]
    if not isinstance(anomalies, list):
        raise ValueError("老板早报 anomalies 必须是数组。")
    if any(not isinstance(item, dict) for item in anomalies):
        raise ValueError("老板早报 anomaly 必须是对象。")
    for item in anomalies:
        item["severity_label"] = _severity_label(item.get("severity"))
    actionable_anomalies = [
        item for item in anomalies if item.get("status") in _ACTIONABLE_STATUSES
    ]
    action = next(
        (
            item for item in contract.get("allowed_actions", [])
            if item.get("id") == "transition_owner_briefing_task"
        ),
        None,
    )
    if actionable_anomalies and action is None:
        raise ValueError("老板早报缺少任务状态 Action 契约。")

    metrics = [
        _metric("异常总数", "metrics.anomaly_count"),
        _metric("紧急", "metrics.critical_count"),
        _metric("重要", "metrics.high_count"),
        _metric("一般", "metrics.medium_count"),
        _metric("逾期应收", "metrics.overdue_receivable_amount", "元"),
    ]
    children = [
        _card("异常概览", {"id": "metrics", "type": "MetricGrid", "props": {}, "children": metrics}),
        _card(
            "早报说明",
            {
                "id": "summary", "type": "DescriptionList",
                "props": {"items": [
                    {"label": "公司", "path": "company_name"},
                    {"label": "日期", "path": "report_date"},
                    {"label": "结论", "path": "summary"},
                ]}, "children": [],
            },
        ),
        _card(
            "状态说明",
            {
                "id": "owner-live-state-notice", "type": "Alert",
                "props": {
                    "path": "live_state_notice",
                    "severity": "info",
                },
                "children": [],
            },
        ),
    ]
    review = data.get("previous_day_review")
    if isinstance(review, dict):
        children.append(
            _card(
                "昨日闭环复盘",
                {
                    "id": "previous-day-review", "type": "MetricGrid", "props": {},
                    "children": [
                        _metric("昨日新增", "previous_day_review.metrics.new_count"),
                        _metric("已完成", "previous_day_review.metrics.completed_count"),
                        _metric("未闭环", "previous_day_review.metrics.unresolved_count"),
                        _metric("已升级", "previous_day_review.metrics.escalated_count"),
                        _metric("今日重复", "previous_day_review.metrics.recurring_count"),
                    ],
                },
            )
        )
        children.append(
            _card(
                "重复问题",
                {
                    "id": "recurring-issues", "type": "DataTable",
                    "props": {
                        "path": "previous_day_review.recurring_issues",
                        "columns": [
                            {"field": "title", "header": "问题"},
                            {"field": "previous_status_label", "header": "昨日状态"},
                            {"field": "assignee", "header": "承接人"},
                        ],
                    },
                    "children": [],
                },
            )
        )
    children.append(
        _card(
            "需要处理的异常",
            {
                "id": "anomalies", "type": "DataTable",
                "props": {"path": "anomalies", "columns": [
                    {"field": "title", "header": "异常事项"},
                    {"field": "severity_label", "header": "优先级"},
                    {"field": "status_label", "header": "状态"},
                    {"field": "owner", "header": "责任人"},
                    {"field": "assignee", "header": "承接人"},
                    {"field": "due_at_local", "header": "截止时间（北京时间）"},
                    {"field": "reminder_count", "header": "提醒次数"},
                    {"field": "escalation_pending_label", "header": "待升级确认"},
                    {"field": "evidence_ref", "header": "证据"},
                ]}, "children": [],
            },
        )
    )
    if actionable_anomalies:
        children.append(
            _card(
                "确认任务处理",
                {
                    "id": "task-form", "type": "Form",
                    "props": {
                        "title": (
                            "请选择异常事件和处理动作；系统会自动关联编号。"
                            "所有动作仅更新本地状态，不会发送外部消息。"
                        )
                    },
                    "children": [
                        _input(
                            "Select",
                            "处理异常",
                            "anomaly_key",
                            [_anomaly_option(item) for item in actionable_anomalies],
                            span="full",
                        ),
                        _input("Select", "处理动作", "action", ["分派", "完成", "升级"]),
                        _input("TextInput", "承接人", "assignee"),
                        _input("TextArea", "处理说明", "note"),
                        {
                            "id": "task-submit", "type": "ActionButton",
                            "props": {"actionId": "transition_owner_briefing_task", "label": "确认更新任务", "tone": "primary"},
                            "children": [],
                        },
                    ],
                },
            )
        )
    spec = {
        "version": "0.1", "scenario": "owner_daily_briefing",
        "interaction_mode": "host_action" if actionable_anomalies else "read_only",
        "title": title.strip(), "summary": artifact_data["summary"],
        "root": {"id": "root", "type": "Column", "props": {}, "children": children},
        "actions": ([{
            "id": "transition_owner_briefing_task", "label": "确认更新任务",
            "tool": "transition_owner_briefing_task", "event_type": "confirm",
            "confirm": True, "tone": "primary",
            "arguments_schema": deepcopy(action["arguments_schema"]),
        }] if actionable_anomalies else []),
        "form_defaults": {
            "anomaly_key": (
                actionable_anomalies[0]["anomaly_key"]
                if actionable_anomalies
                else ""
            ),
            "action": "分派" if actionable_anomalies else "",
            "assignee": "",
            "note": "",
        },
    }
    return {
        "type": "generation_ui", "artifact_id": str(uuid4()),
        "scenario": "owner_daily_briefing", "text": artifact_data["summary"],
        "spec": spec, "data": artifact_data,
    }


def _card(title: str, child: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"card-{len(title)}-{title}", "type": "Card", "props": {"title": title}, "children": [child]}


def _metric(label: str, path: str, suffix: str | None = None) -> dict[str, Any]:
    props = {"label": label, "path": path, "tone": "default"}
    if suffix:
        props["suffix"] = suffix
    return {"id": f"metric-{path}", "type": "Metric", "props": props, "children": []}


def _input(
    component: str,
    label: str,
    model: str,
    options: list[str | dict[str, str]] | None = None,
    *,
    span: str | None = None,
) -> dict[str, Any]:
    props: dict[str, Any] = {"label": label, "model": model}
    if options is not None:
        props["options"] = options
    if span is not None:
        props["span"] = span
    return {"id": f"input-{model}", "type": component, "props": props, "children": []}


def _severity_label(value: Any) -> str:
    text = str(value or "").strip()
    return _SEVERITY_LABELS.get(text, text or "未分级")


def _anomaly_option(item: dict[str, Any]) -> dict[str, str]:
    severity = _severity_label(item.get("severity"))
    status = (
        "待升级"
        if item.get("escalation_pending")
        else str(item.get("status_label") or "").strip()
    )
    title = str(item.get("title") or "未命名异常").strip()
    prefix = f"{severity} · {status}" if status else severity
    return {
        "label": f"【{prefix}】{title}",
        "value": str(item["anomaly_key"]),
    }


__all__ = ["build_owner_briefing_ui_artifact"]
