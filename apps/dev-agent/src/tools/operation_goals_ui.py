"""14.1 目标的只读内容与用户确认按钮，复用现有 Generation UI。"""

from copy import deepcopy
from typing import Any
from uuid import uuid4

from tools.operation_goals import GOAL_SCENARIO, GoalHandoff


def build_operation_goal_ui(handoff: dict[str, Any]) -> dict[str, Any]:
    data = GoalHandoff.model_validate({key: handoff[key] for key in ("goal_ref", "goal_snapshot")}).model_dump(mode="json")
    snapshot = data["goal_snapshot"]
    goal = snapshot["operation_goal"]
    pending = snapshot["confirmation"]["status"] == "pending_confirmation"
    data.update({
        "company_name": snapshot["company_name"], "company_profile": snapshot["company_profile"],
        "version_label": f"第 {snapshot['version']} 版",
        "summary": goal["summary"], "period": f"{goal['period']['start']} 至 {goal['period']['end']}",
        "business_direction": goal["business_direction"],
        "target_users": "；".join(goal["target_users"]),
        "metrics": [{**item, "baseline": item["baseline"] or "暂无可靠基线"} for item in goal["metrics"]],
        "budget_statement": goal["budget_statement"],
        "status_label": "待确认" if pending else "已记录此版本的对话确认",
        "boundary": "此操作仅确认目标版本，不代表预算获批，也不会开始研究或付费执行。",
        **{name: "；".join(goal[name]) or "暂无记录" for name in (
            "resources", "constraints", "evidence", "assumptions", "risks"
        )},
    })
    actions = [{
        "id": "confirm_operation_goal", "label": "确认此版本目标",
        "tool": "confirm_operation_goal", "event_type": "confirm", "confirm": True,
        "tone": "primary", "arguments_schema": {
            "type": "object", "properties": {}, "required": [], "additionalProperties": False,
        },
    }]
    children = [{
        "id": "goal-details", "type": "DescriptionList", "props": {"items": [
            {"label": label, "path": path} for label, path in (
                ("企业", "company_name"), ("企业背景", "company_profile"),
                ("版本", "version_label"), ("周期", "period"), ("本期目标", "summary"),
                ("业务方向", "business_direction"), ("目标用户", "target_users"),
                ("人员与资源", "resources"), ("约束", "constraints"), ("预算情况", "budget_statement"),
                ("依据", "evidence"), ("待验证假设", "assumptions"), ("风险", "risks"),
                ("确认状态", "status_label"), ("目标文件", "goal_ref.artifact_path"),
            )
        ]}, "children": [],
    }, {
        "id": "goal-metrics", "type": "DataTable", "props": {
            "path": "metrics", "columns": [
                {"field": field, "header": label} for field, label in (
                    ("name", "指标"), ("definition", "统计口径"), ("baseline", "历史基线"),
                    ("target", "本期目标"), ("data_source", "数据来源"), ("review_frequency", "检查频率"),
                )
            ],
        }, "children": [],
    }, {
        "id": "goal-boundary", "type": "Alert", "props": {"path": "boundary", "severity": "info"}, "children": [],
    }]
    week = snapshot.get("weekly_goal")
    if week:
        data["weekly_summary"] = (f"{week['period']['start']} 至 {week['period']['end']}："
            + week["summary"] + "；与月目标的关系：" + week["contribution"])
        data["weekly_metrics"] = week["metrics"]
        data["boundary"] = "确认本版同时确认月目标和简单周目标；后续周计划使用周目标。" + data["boundary"]
        children.insert(2, {"id": "weekly-goal-details", "type": "DescriptionList", "props": {
            "items": [{"label": "简单周目标", "path": "weekly_summary"}]}, "children": []})
        children.insert(3, {"id": "weekly-goal-metrics", "type": "DataTable", "props": {
            "path": "weekly_metrics", "columns": children[1]["props"]["columns"]}, "children": []})
    if pending:
        children.append({
            "id": "goal-confirm", "type": "ActionButton", "props": {
                "actionId": "confirm_operation_goal", "label": "确认此版本目标", "tone": "primary",
            }, "children": [],
        })
    return {
        "type": "generation_ui", "artifact_id": str(uuid4()), "scenario": GOAL_SCENARIO,
        "text": data["status_label"], "data": deepcopy(data),
        "spec": {
            "version": "0.1", "scenario": GOAL_SCENARIO,
            "interaction_mode": "host_action" if pending else "read_only",
            "title": "小红书运营目标", "summary": data["status_label"],
            "root": {"id": "root", "type": "Column", "props": {}, "children": children},
            "actions": actions, "form_defaults": {},
        },
    }
