"""获客内容运营 V0 的受控 Generation UI。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4


CONTENT_UI_SCENARIOS = {"content_weekly_plan", "content_performance_review"}
LEGACY_RECOVERY_SCHEMA = {"type": "object", "properties": {
    "content_id": {"type": "string", "minLength": 1, "maxLength": 100},
    "resolution": {"type": "string", "enum": ["人工确认存在", "人工确认未保存"]},
    "note": {"type": "string", "minLength": 1, "maxLength": 2000}},
    "required": ["content_id", "resolution"], "additionalProperties": False}


def with_content_recovery(spec):
    """为新旧卡片加入宿主固定的人工核验入口，不授予模型执行权限。"""
    spec = deepcopy(spec)
    if any(action["id"] == "resolve_xiaohongshu_draft" for action in spec.get("actions", [])):
        return spec
    spec["actions"].append({"id": "resolve_xiaohongshu_draft", "tool": "resolve_xiaohongshu_draft",
        "label": "保存人工草稿核验", "event_type": "confirm", "confirm": True, "tone": "primary",
        "arguments_schema": deepcopy(LEGACY_RECOVERY_SCHEMA)})
    spec["form_defaults"]["resolution"] = "人工确认存在"
    spec["root"]["children"].append(_card("结果不明时人工核验", {"id": "content-recovery-form", "type": "Form",
        "props": {"title": "先人工打开草稿箱核对。确认未保存后仅解除原任务并重新待审核，不自动执行。"}, "children": [
            _select("recovery-resolution", "人工核验结论", "resolution", ["人工确认存在", "人工确认未保存"]),
            _textarea("recovery-note", "核验说明（必填）", "note"),
            _button("recovery-submit", "resolve_xiaohongshu_draft", "保存人工草稿核验")]}))
    return spec


def build_content_operations_ui_artifact(
    scenario: str,
    title: str,
    data: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    if scenario not in CONTENT_UI_SCENARIOS:
        raise ValueError(f"不支持的内容运营 UI 场景：{scenario}。")
    safe_title = _text(title, "title", 80)
    if scenario == "content_weekly_plan":
        spec, summary = _weekly_plan_spec(safe_title, data, contract)
    else:
        spec, summary = _performance_spec(safe_title, data)
    return {
        "type": "generation_ui",
        "artifact_id": str(uuid4()),
        "scenario": scenario,
        "text": summary,
        "spec": spec,
        "data": deepcopy(data),
    }


def _weekly_plan_spec(
    title: str,
    data: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    _require_keys(
        data,
        {"company_name", "week_start", "summary", "metrics", "contents"},
    )
    contents = data["contents"]
    if not isinstance(contents, list) or not contents:
        raise ValueError("内容计划必须包含选题。")
    actions = {
        item.get("id"): item
        for item in contract.get("allowed_actions", [])
        if isinstance(item, dict)
    }
    if not {"review_content_plan", "save_xiaohongshu_draft"} <= set(actions):
        raise ValueError("内容计划缺少审核或小红书草稿 Action。")
    content_ids = [item["content_id"] for item in contents]
    summary = data["summary"]
    children = [
        _card(
            "本周内容概览",
            {
                "id": "content-plan-metrics",
                "type": "MetricGrid",
                "props": {},
                "children": [
                    _metric("选题数", "metrics.content_count"),
                    _metric("待审核", "metrics.pending_review_count"),
                    _metric("已审核", "metrics.approved_count"),
                    _metric("草稿已保存", "metrics.draft_saved_count"),
                ],
            },
        ),
        _card(
            "来源与安全边界",
            {
                "id": "content-source-notice",
                "type": "Alert",
                "props": {"path": "source_notice", "severity": "info"},
                "children": [],
            },
        ),
        _card(
            "每周选题池",
            _table(
                "content-plan-table",
                "contents",
                [
                    {"field": "content_id", "header": "内容编号"},
                    {"field": "publish_day", "header": "日期"},
                    {"field": "title", "header": "标题"},
                    {"field": "audience", "header": "目标人群"},
                    {"field": "goal", "header": "目标"},
                    {"field": "status_label", "header": "审核状态"},
                    {"field": "rpa_status_label", "header": "小红书草稿"},
                    {"field": "source_title", "header": "参考来源"},
                ],
            ),
        ),
        _card(
            "文案、脚本与素材",
            _table(
                "content-detail-table",
                "contents",
                [
                    {"field": "content_id", "header": "编号"},
                    {"field": "title", "header": "标题"},
                    {"field": "body", "header": "正文"},
                    {"field": "tags_text", "header": "标签"},
                    {"field": "video_script", "header": "短视频脚本"},
                    {"field": "missing_assets_text", "header": "待补素材"},
                ],
            ),
        ),
        _card(
            "人工审核",
            {
                "id": "content-review-form",
                "type": "Form",
                "props": {"title": "确认内容前请选择 1～9 张图片；图片只保存在本机受控目录。"},
                "children": [
                    _select("review-content-id", "内容编号", "content_id", content_ids),
                    _select(
                        "review-decision",
                        "审核决定",
                        "decision",
                        ["确认内容", "要求修改", "放弃选题"],
                    ),
                    _textarea("review-note", "审核说明", "note"),
                    _textarea(
                        "review-image-paths",
                        "已选图片路径（可手工调整，每行一张）",
                        "image_paths_text",
                    ),
                    _button("review-content-submit", "review_content_plan", "保存审核结果"),
                ],
            },
        ),
        _card(
            "保存到小红书草稿箱",
            {
                "id": "content-rpa-form",
                "type": "Form",
                "props": {
                    "title": "仅限已审核内容；会控制专用 Chrome 保存草稿，但绝不最终发布。"
                },
                "children": [
                    _button(
                        "save-xhs-draft",
                        "save_xiaohongshu_draft",
                        "确认启动 RPA 保存草稿",
                        tone="danger",
                    )
                ],
            },
        ),
    ]
    spec_actions = [
        {
            "id": action_id,
            "label": (
                "保存审核结果"
                if action_id == "review_content_plan"
                else "启动 RPA 保存小红书草稿"
            ),
            "tool": action_id,
            "event_type": "confirm",
            "confirm": True,
            "tone": "primary" if action_id == "review_content_plan" else "danger",
            "arguments_schema": deepcopy(actions[action_id]["arguments_schema"]),
        }
        for action_id in ("review_content_plan", "save_xiaohongshu_draft")
    ]
    return (
        with_content_recovery(_spec(
            "content_weekly_plan",
            title,
            summary,
            children,
            actions=spec_actions,
            form_defaults={
                "content_id": content_ids[0],
                "decision": "确认内容",
                "note": "",
                "image_paths_text": "",
            },
        )),
        summary,
    )


def _performance_spec(title: str, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    _require_keys(data, {"summary", "metrics", "contents", "next_week_actions"})
    summary = data["summary"]
    children = [
        _card(
            "表现概览",
            {
                "id": "performance-metrics",
                "type": "MetricGrid",
                "props": {},
                "children": [
                    _metric("内容数", "metrics.content_count"),
                    _metric("总曝光", "metrics.total_impressions"),
                    _metric("咨询", "metrics.total_inquiries"),
                    _metric("线索", "metrics.total_leads"),
                    _metric("建议小额投流", "metrics.建议小额投流"),
                    _metric("建议停止", "metrics.建议停止"),
                ],
            },
        ),
        _card(
            "逐条判断",
            _table(
                "performance-table",
                "contents",
                [
                    {"field": "content_id", "header": "内容编号"},
                    {"field": "title", "header": "标题"},
                    {"field": "impressions", "header": "曝光"},
                    {"field": "inquiries", "header": "咨询"},
                    {"field": "leads", "header": "线索"},
                    {"field": "engagement_rate", "header": "互动率", "format": "percent"},
                    {"field": "decision", "header": "建议"},
                    {"field": "reason", "header": "依据"},
                ],
            ),
        ),
        _card(
            "下周动作",
            {
                "id": "next-week-actions",
                "type": "RecommendationList",
                "props": {"path": "next_week_actions"},
                "children": [],
            },
        ),
        _card(
            "投流边界",
            {
                "id": "spend-boundary",
                "type": "Alert",
                "props": {"path": "spend_boundary", "severity": "warning"},
                "children": [],
            },
        ),
    ]
    return _spec("content_performance_review", title, summary, children), summary


def _spec(
    scenario: str,
    title: str,
    summary: str,
    children: list[dict[str, Any]],
    *,
    actions: list[dict[str, Any]] | None = None,
    form_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_actions = actions or []
    return {
        "version": "0.1",
        "scenario": scenario,
        "interaction_mode": "host_action" if safe_actions else "read_only",
        "title": title,
        "summary": summary,
        "root": {"id": "root", "type": "Column", "props": {}, "children": children},
        "actions": safe_actions,
        "form_defaults": form_defaults or {},
    }


def _card(title: str, child: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"card-{child['id']}",
        "type": "Card",
        "props": {"title": title},
        "children": [child],
    }


def _table(identifier: str, path: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": identifier,
        "type": "DataTable",
        "props": {"path": path, "columns": columns},
        "children": [],
    }


def _metric(label: str, path: str) -> dict[str, Any]:
    return {
        "id": f"metric-{path}",
        "type": "Metric",
        "props": {"label": label, "path": path, "tone": "default"},
        "children": [],
    }


def _select(identifier: str, label: str, model: str, options: list[str]) -> dict[str, Any]:
    return {
        "id": identifier,
        "type": "Select",
        "props": {"label": label, "model": model, "options": options},
        "children": [],
    }


def _textarea(identifier: str, label: str, model: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "type": "TextArea",
        "props": {"label": label, "model": model, "placeholder": ""},
        "children": [],
    }


def _button(
    identifier: str,
    action_id: str,
    label: str,
    *,
    tone: str = "primary",
) -> dict[str, Any]:
    return {
        "id": identifier,
        "type": "ActionButton",
        "props": {"actionId": action_id, "label": label, "tone": tone},
        "children": [],
    }


def _require_keys(data: Any, keys: set[str]) -> None:
    if not isinstance(data, dict) or keys - set(data):
        raise ValueError("内容运营 UI 数据不完整。")


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{name} 必须是 1-{limit} 个字符。")
    return value.strip()


__all__ = ["CONTENT_UI_SCENARIOS", "build_content_operations_ui_artifact"]
