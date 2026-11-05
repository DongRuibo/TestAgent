"""14.3 选题审核：不制作正文、不上传图片、不启动 RPA。"""

from copy import deepcopy
from uuid import uuid4

from tools.content_operations_ui import _button, _card, _select, _spec, _table, _textarea


TOPIC_PLAN_SCENARIO = "content_topic_plan"
REVIEW_TOPIC_ACTION = "review_content_topic"
TOPIC_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "content_id": {"type": "string", "minLength": 1, "maxLength": 100},
        "decision": {"type": "string", "enum": ["确认选题", "要求修改", "放弃选题"]},
        "note": {"type": "string", "maxLength": 2000},
    },
    "required": ["content_id", "decision"], "additionalProperties": False,
}


def build_content_topic_plan_ui(plan):
    data = deepcopy(plan)
    data["limitations_text"] = "；".join(data["research_limitations"]) or "未记录额外局限，仍需核实来源。"
    data["goal_version_label"] = (
        f"第 {data['goal_ref']['version']} 版" if data.get("goal_ref") else "旧版或显式文本输入，无已确认目标版本"
    )
    for item in data["contents"]:
        item["materials_text"] = "；".join(item["required_materials"])
        item["risks_text"] = "；".join(item["risks"])
        item["evidence_text"] = "；".join(
            f"{entry['fact']}（来源：{', '.join(entry['source_refs'])}）" for entry in item["evidence"]
        )
    ids = [item["content_id"] for item in data["contents"]]
    children = [
        {"id": "topic-context", "type": "DescriptionList", "props": {"items": [
            {"label": label, "path": path} for label, path in (
                ("企业", "company_name"), ("企业背景", "company_profile"), ("周一日期", "week_start"),
                ("运营目标", "weekly_goal"), ("目标版本", "goal_version_label"),
                ("研究状态", "research_execution_status"), ("研究文件", "research_artifact_path"),
                ("计划状态", "approval_status"), ("操作边界", "rpa_boundary"),
            )
        ]}, "children": []},
        {"id": "topic-limitations", "type": "Alert", "props": {
            "path": "limitations_text", "severity": "warning",
        }, "children": []},
        _card("本周选题及审核记录", _table("topic-table", "contents", [
            {"field": field, "header": label} for field, label in (
                ("content_id", "编号"), ("publish_day", "日期"), ("title", "选题"),
                ("audience", "受众"), ("goal", "目标"), ("why_fit_company", "企业匹配原因"),
                ("evidence_text", "研究依据"), ("materials_text", "素材需求"),
                ("risks_text", "风险"), ("status_label", "审核状态"), ("review_note", "审核说明"),
            )
        ])),
        _card("原始来源", _table("topic-sources", "reference_sources", [
            {"field": field, "header": label} for field, label in (
                ("source_id", "来源编号"), ("url", "来源链接"),
            )
        ])),
        _card("人工选题审核", {"id": "topic-review-form", "type": "Form", "props": {
            "title": "仅确认选题；要求修改必须填写说明。没有正文和图片，不提供草稿箱操作。",
        }, "children": [
            _select("topic-id", "选题编号", "content_id", ids),
            _select("topic-decision", "审核决定", "decision", ["确认选题", "要求修改", "放弃选题"]),
            _textarea("topic-note", "审核说明", "note"),
            _button("topic-submit", REVIEW_TOPIC_ACTION, "保存选题审核"),
        ]}),
    ]
    spec = _spec(TOPIC_PLAN_SCENARIO, "小红书周计划 · 选题审核", data["summary"], children,
        actions=[{"id": REVIEW_TOPIC_ACTION, "tool": REVIEW_TOPIC_ACTION, "label": "保存选题审核",
            "event_type": "confirm", "confirm": True, "tone": "primary",
            "arguments_schema": deepcopy(TOPIC_REVIEW_SCHEMA)}],
        form_defaults={"content_id": ids[0], "decision": "确认选题", "note": ""})
    return {"type": "generation_ui", "artifact_id": str(uuid4()), "scenario": TOPIC_PLAN_SCENARIO,
        "text": data["summary"], "spec": spec, "data": data}
