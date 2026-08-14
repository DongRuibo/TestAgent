"""装修业务场景的受控 UISpec Builder。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4


RENOVATION_SCENARIOS = {
    "renovation_project_create",
    "material_quantity",
    "mcp_material_estimate",
    "quote_comparison",
    "construction_schedule",
    "contract_review",
}


def build_renovation_ui_artifact(
    scenario: str,
    title: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    if scenario not in RENOVATION_SCENARIOS:
        raise ValueError(f"不支持的装修 UI 场景：{scenario}。")
    safe_title = _text(title, "title", 80)
    builders = {
        "renovation_project_create": _project_create_spec,
        "material_quantity": _material_quantity_spec,
        "mcp_material_estimate": _mcp_material_estimate_spec,
        "quote_comparison": _quote_comparison_spec,
        "construction_schedule": _construction_schedule_spec,
        "contract_review": _contract_review_spec,
    }
    spec, summary = builders[scenario](safe_title, data)
    return {
        "type": "generation_ui",
        "artifact_id": str(uuid4()),
        "scenario": scenario,
        "text": summary,
        "spec": spec,
        "data": deepcopy(data),
    }


def _project_create_spec(
    title: str, data: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    _require_keys(data, {"city", "area_m2", "style", "status"})
    summary = "项目草稿已准备好，请填写完整地址并确认创建。"
    root = _column(
        [
            _card(
                "project-summary-card",
                "项目草稿",
                {
                    "id": "project-summary",
                    "type": "DescriptionList",
                    "props": {
                        "items": [
                            {"label": "城市", "path": "city"},
                            {"label": "面积", "path": "area_m2", "suffix": "㎡"},
                            {"label": "风格", "path": "style"},
                            {"label": "状态", "path": "status"},
                        ]
                    },
                    "children": [],
                },
            ),
            _card(
                "address-card",
                "确认地址",
                {
                    "id": "address-form",
                    "type": "Form",
                    "props": {"title": "请输入完整施工地址"},
                    "children": [
                        {
                            "id": "project-address",
                            "type": "TextInput",
                            "props": {
                                "label": "施工地址",
                                "model": "address",
                                "placeholder": f"例如：{data['city']}西湖区……",
                            },
                            "children": [],
                        },
                        {
                            "id": "confirm-project",
                            "type": "ActionButton",
                            "props": {
                                "actionId": "confirm_create_project",
                                "label": "确认创建项目",
                                "tone": "primary",
                            },
                            "children": [],
                        },
                    ],
                },
            ),
        ]
    )
    return (
        _spec(
            "renovation_project_create",
            title,
            summary,
            root,
            interaction_mode="host_action",
            actions=[
                {
                    "id": "confirm_create_project",
                    "label": "确认创建项目",
                    "tool": "create_renovation_project",
                    "event_type": "confirm",
                    "confirm": True,
                    "tone": "primary",
                    "arguments_schema": {
                        "type": "object",
                        "properties": {
                            "address": {
                                "type": "string",
                                "minLength": 5,
                                "maxLength": 200,
                            }
                        },
                        "required": ["address"],
                        "additionalProperties": False,
                    },
                }
            ],
            form_defaults={"address": ""},
        ),
        summary,
    )


def _material_quantity_spec(
    title: str, data: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    _require_keys(data, {"metrics", "rooms", "assumptions"})
    summary = "已按房间尺寸和明确损耗假设计算材料采购量。"
    root = _column(
        [
            _card(
                "material-metrics-card",
                "材料汇总",
                {
                    "id": "material-metrics",
                    "type": "MetricGrid",
                    "props": {},
                    "children": [
                        _metric("paint", "墙漆", "metrics.paint_liters", suffix="L"),
                        _metric("tiles", "瓷砖", "metrics.tile_area_m2", suffix="㎡"),
                        _metric("flooring", "地板", "metrics.flooring_area_m2", suffix="㎡"),
                    ],
                },
            ),
            _card(
                "room-material-card",
                "分房间计算",
                _table(
                    "room-materials",
                    "rooms",
                    [
                        {"field": "name", "header": "房间"},
                        {"field": "wall_area_m2", "header": "净墙面㎡"},
                        {"field": "floor_area_m2", "header": "地面㎡"},
                        {"field": "paint_liters", "header": "墙漆L"},
                        {"field": "tile_area_m2", "header": "瓷砖㎡"},
                        {"field": "flooring_area_m2", "header": "地板㎡"},
                    ],
                ),
            ),
            _card(
                "material-assumptions-card",
                "计算假设",
                {
                    "id": "material-assumptions",
                    "type": "DescriptionList",
                    "props": {
                        "items": [
                            {"label": "墙漆覆盖率", "path": "assumptions.paint_coverage_m2_per_liter"},
                            {"label": "涂刷遍数", "path": "assumptions.paint_coats"},
                            {"label": "瓷砖损耗", "path": "assumptions.tile_waste_rate", "format": "percent"},
                            {"label": "地板损耗", "path": "assumptions.flooring_waste_rate", "format": "percent"},
                        ]
                    },
                    "children": [],
                },
            ),
        ]
    )
    return _spec("material_quantity", title, summary, root), summary


def _mcp_material_estimate_spec(
    title: str, data: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    _require_keys(data, {"metrics", "details", "assumptions", "notes", "tools"})
    metrics = data["metrics"]
    if not isinstance(metrics, dict) or not metrics:
        raise ValueError("MCP 材料指标不能为空。")
    metric_definitions = (
        ("net_wall_area_m2", "净墙面", "㎡"),
        ("floor_area_m2", "地面", "㎡"),
        ("primer_liters", "底漆采购", "L"),
        ("finish_liters", "面漆采购", "L"),
        ("tile_boxes", "瓷砖采购", "箱"),
        ("flooring_packages", "地板采购", "包"),
        ("skirting_pieces", "踢脚线", "根"),
    )
    metric_nodes = [
        _metric(key, label, f"metrics.{key}", suffix=suffix)
        for key, label, suffix in metric_definitions
        if key in metrics
    ]
    summary = "已汇总本轮远程 MCP 返回的面积和材料采购计算结果。"
    children: list[dict[str, Any]] = [
        _card(
            "mcp-material-metrics-card",
            "面积与采购量",
            {
                "id": "mcp-material-metrics",
                "type": "MetricGrid",
                "props": {},
                "children": metric_nodes,
            },
        ),
        _card(
            "mcp-material-details-card",
            "计算明细",
            _table(
                "mcp-material-details",
                "details",
                [
                    {"field": "category", "header": "类别"},
                    {"field": "item", "header": "项目"},
                    {"field": "result", "header": "结果"},
                ],
            ),
        ),
    ]
    if data["assumptions"]:
        children.append(
            _card(
                "mcp-material-assumptions-card",
                "输入与假设",
                _table(
                    "mcp-material-assumptions",
                    "assumptions",
                    [
                        {"field": "item", "header": "参数"},
                        {"field": "value", "header": "取值"},
                    ],
                ),
            )
        )
    if data["notes"]:
        children.append(
            _card(
                "mcp-material-notes-card",
                "使用说明",
                {
                    "id": "mcp-material-notes",
                    "type": "RecommendationList",
                    "props": {"path": "notes"},
                    "children": [],
                },
            )
        )
    return _spec("mcp_material_estimate", title, summary, _column(children)), summary


def _quote_comparison_spec(
    title: str, data: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    _require_keys(data, {"companies", "items", "anomalies"})
    summary = "已统一核算报价，并标记缺项、算术不一致和价格偏离。"
    children = [
        _card(
            "quote-companies-card",
            "公司总价",
            _table(
                "quote-companies",
                "companies",
                [
                    {"field": "company", "header": "公司"},
                    {"field": "total", "header": "总价", "format": "currency"},
                    {"field": "item_count", "header": "项目数"},
                ],
            ),
        ),
        _card(
            "quote-items-card",
            "逐项报价",
            {
                "id": "quote-comparison",
                "type": "ComparisonTable",
                "props": {
                    "path": "items",
                    "titleField": "item",
                    "fields": data["comparison_fields"],
                    "interactive": True,
                },
                "children": [],
            },
        ),
    ]
    if data["anomalies"]:
        children.append(
            _card(
                "quote-anomalies-card",
                "异常项",
                _table(
                    "quote-anomalies",
                    "anomalies",
                    [
                        {"field": "company", "header": "公司"},
                        {"field": "item", "header": "项目"},
                        {"field": "type", "header": "异常"},
                        {"field": "detail", "header": "说明"},
                    ],
                ),
            )
        )
    return _spec("quote_comparison", title, summary, _column(children)), summary


def _construction_schedule_spec(
    title: str, data: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    _require_keys(data, {"city", "start_date", "tasks", "weather_status", "warnings"})
    summary = f"已从 {data['start_date']} 起安排施工，并标注天气核验状态。"
    children: list[dict[str, Any]] = [
        _card(
            "schedule-status-card",
            "排期状态",
            {
                "id": "weather-status",
                "type": "Alert",
                "props": {
                    "title": "天气数据状态",
                    "path": "weather_status",
                    "severity": "info" if data["weather_status"] == "已核验" else "warn",
                },
                "children": [],
            },
        ),
        _card(
            "schedule-timeline-card",
            "施工时间线",
            {
                "id": "schedule-timeline",
                "type": "Timeline",
                "props": {
                    "path": "tasks",
                    "titleField": "name",
                    "dateField": "date_range",
                    "descriptionField": "note",
                },
                "children": [],
            },
        ),
    ]
    if data["warnings"]:
        children.append(
            _card(
                "schedule-warning-card",
                "天气风险",
                {"id": "schedule-warnings", "type": "RecommendationList", "props": {"path": "warnings"}, "children": []},
            )
        )
    return _spec("construction_schedule", title, summary, _column(children)), summary


def _contract_review_spec(
    title: str, data: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    _require_keys(data, {"risks", "source_count", "disclaimer"})
    summary = f"已基于 {data['source_count']} 条合同原文证据生成规则审查结果。"
    children: list[dict[str, Any]] = [
        _card(
            "contract-disclaimer-card",
            "使用说明",
            {"id": "contract-disclaimer", "type": "Alert", "props": {"path": "disclaimer", "severity": "warn"}, "children": []},
        )
    ]
    if data["risks"]:
        children.append(
            _card(
                "contract-risks-card",
                "风险条款",
                _table(
                    "contract-risks",
                    "risks",
                    [
                        {"field": "severity", "header": "等级"},
                        {"field": "title", "header": "风险"},
                        {"field": "evidence", "header": "合同原文"},
                        {"field": "source", "header": "来源"},
                        {"field": "recommendation", "header": "建议"},
                    ],
                ),
            )
        )
    else:
        children.append(
            _card(
                "contract-empty-card",
                "审查结果",
                {"id": "contract-empty", "type": "EmptyState", "props": {"title": "证据中未命中预设风险规则", "description": "这不代表合同没有其他法律风险，建议继续人工复核。"}, "children": []},
            )
        )
    return _spec("contract_review", title, summary, _column(children)), summary


def _spec(
    scenario: str,
    title: str,
    summary: str,
    root: dict[str, Any],
    *,
    interaction_mode: str = "read_only",
    actions: list[dict[str, Any]] | None = None,
    form_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "version": "0.1",
        "scenario": scenario,
        "interaction_mode": interaction_mode,
        "title": title,
        "summary": summary,
        "root": root,
        "actions": actions or [],
        "form_defaults": form_defaults or {},
    }


def _column(children: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": "root", "type": "Column", "props": {}, "children": children}


def _card(node_id: str, title: str, child: dict[str, Any]) -> dict[str, Any]:
    return {"id": node_id, "type": "Card", "props": {"title": title}, "children": [child]}


def _table(node_id: str, path: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": node_id, "type": "DataTable", "props": {"path": path, "columns": columns}, "children": []}


def _metric(
    node_id: str, label: str, path: str, *, suffix: str | None = None
) -> dict[str, Any]:
    props: dict[str, Any] = {"label": label, "path": path, "tone": "default"}
    if suffix:
        props["suffix"] = suffix
    return {"id": node_id, "type": "Metric", "props": props, "children": []}


def _require_keys(data: dict[str, Any], required: set[str]) -> None:
    if not isinstance(data, dict):
        raise ValueError("装修 UI 数据必须是对象。")
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"装修 UI 数据缺少字段：{', '.join(missing)}。")


def _text(value: Any, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串。")
    normalized = value.strip()
    if len(normalized) > max_chars:
        raise ValueError(f"{field} 最多包含 {max_chars} 个字符。")
    return normalized
