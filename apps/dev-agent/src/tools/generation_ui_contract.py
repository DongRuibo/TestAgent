"""Generation UI 的通用交互契约。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


INTERACTION_TYPES = {
    "collect_input",
    "compare",
    "visualize",
    "action",
    "confirm",
}

UI_CAPABILITIES = {
    "alert",
    "chart",
    "comparison",
    "description",
    "form",
    "metrics",
    "recommendations",
    "table",
    "timeline",
}

_SCENARIO_DEFAULTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "sales_briefing": ("visualize", ("metrics", "chart", "table")),
    "owner_daily_briefing": (
        "confirm",
        ("metrics", "description", "table", "form"),
    ),
    "content_weekly_plan": (
        "confirm",
        ("metrics", "table", "recommendations", "form"),
    ),
    "content_performance_review": (
        "visualize",
        ("metrics", "table", "recommendations", "alert"),
    ),
    "renovation_project_create": ("confirm", ("description", "form")),
    "material_quantity": ("visualize", ("metrics", "table", "description")),
    "mcp_material_estimate": (
        "visualize",
        ("metrics", "table", "recommendations"),
    ),
    "quote_comparison": ("compare", ("comparison", "table")),
    "construction_schedule": ("visualize", ("alert", "timeline", "recommendations")),
    "contract_review": ("compare", ("alert", "table")),
}


def default_generation_ui_contract(
    scenario: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """给已有业务场景补齐统一的交互元数据。"""
    interaction_type, capabilities = _SCENARIO_DEFAULTS.get(
        scenario,
        ("visualize", ("description",)),
    )
    actions: list[dict[str, Any]] = []
    if scenario == "owner_daily_briefing":
        anomaly_keys = [
            item.get("anomaly_key")
            for item in data.get("anomalies", [])
            if isinstance(item, dict) and isinstance(item.get("anomaly_key"), str)
        ]
        actions.append(
            {
                "id": "transition_owner_briefing_task",
                "event_type": "confirm",
                "external_side_effect": False,
                "arguments_schema": {
                    "type": "object",
                    "properties": {
                        "anomaly_key": {"type": "string", "enum": anomaly_keys},
                        "action": {
                            "type": "string",
                            "enum": ["分派", "完成", "升级"],
                        },
                        "assignee": {"type": "string", "maxLength": 80},
                        "note": {"type": "string", "maxLength": 500},
                    },
                    "required": ["anomaly_key", "action"],
                    "additionalProperties": False,
                },
            }
        )
    if scenario == "content_weekly_plan":
        from tools.content_operations_ui import LEGACY_RECOVERY_SCHEMA
        content_ids = [
            item.get("content_id")
            for item in data.get("contents", [])
            if isinstance(item, dict) and isinstance(item.get("content_id"), str)
        ]
        shared_properties = {
            "content_id": {"type": "string", "enum": content_ids},
            "decision": {
                "type": "string",
                "enum": ["确认内容", "要求修改", "放弃选题"],
            },
            "note": {"type": "string", "maxLength": 500},
            "image_paths_text": {
                "type": "string",
                "format": "multiline",
                "maxLength": 5000,
            },
        }
        actions.extend(
            [
                {
                    "id": "review_content_plan",
                    "event_type": "confirm",
                    "external_side_effect": False,
                    "arguments_schema": {
                        "type": "object",
                        "properties": deepcopy(shared_properties),
                        "required": ["content_id", "decision"],
                        "additionalProperties": False,
                    },
                },
                {
                    "id": "save_xiaohongshu_draft",
                    "event_type": "confirm",
                    "external_side_effect": True,
                    "arguments_schema": {
                        "type": "object",
                        "properties": deepcopy(shared_properties),
                        "required": ["content_id"],
                        "additionalProperties": False,
                    },
                },
            ]
        )
        actions.append({"id": "resolve_xiaohongshu_draft", "event_type": "confirm", "external_side_effect": False,
            "arguments_schema": deepcopy(LEGACY_RECOVERY_SCHEMA)})
    if scenario == "renovation_project_create":
        actions.append(
            {
                "id": "confirm_create_project",
                "event_type": "confirm",
                "external_side_effect": True,
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
        )
    return validate_generation_ui_contract(
        {
            "interaction_type": interaction_type,
            "ui_capabilities": list(capabilities),
            "data_schema": infer_data_schema(data),
            "allowed_actions": actions,
        }
    )


def structured_input_contract(fields: list[dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in fields:
        field_schema: dict[str, Any]
        field_type = field["type"]
        if field_type == "number":
            field_schema = {"type": "number"}
            if "min" in field:
                field_schema["minimum"] = field["min"]
            if "max" in field:
                field_schema["maximum"] = field["max"]
        elif field_type == "boolean":
            field_schema = {"type": "boolean"}
        else:
            field_schema = {"type": "string", "maxLength": 500}
            if field_type == "select":
                field_schema["enum"] = list(field["options"])
            elif field_type == "date":
                field_schema["format"] = "date"
        properties[field["name"]] = field_schema
        if field.get("required", True):
            required.append(field["name"])
    arguments_schema = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    return validate_generation_ui_contract(
        {
            "interaction_type": "collect_input",
            "ui_capabilities": ["form"],
            "data_schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "submission": arguments_schema,
                },
                "required": ["status"],
            },
            "allowed_actions": [
                {
                    "id": "submit_structured_input",
                    "event_type": "submit",
                    "external_side_effect": False,
                    "arguments_schema": arguments_schema,
                }
            ],
        }
    )


def validate_generation_ui_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(contract, dict):
        raise ValueError("Generation UI contract 必须是对象。")
    interaction_type = contract.get("interaction_type")
    if interaction_type not in INTERACTION_TYPES:
        raise ValueError("Generation UI interaction_type 不受支持。")
    capabilities = contract.get("ui_capabilities")
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or len(capabilities) > 12
        or any(item not in UI_CAPABILITIES for item in capabilities)
    ):
        raise ValueError("Generation UI ui_capabilities 不受支持。")
    data_schema = contract.get("data_schema")
    if not isinstance(data_schema, dict) or data_schema.get("type") != "object":
        raise ValueError("Generation UI data_schema 必须是 object schema。")
    allowed_actions = contract.get("allowed_actions")
    if not isinstance(allowed_actions, list) or len(allowed_actions) > 10:
        raise ValueError("Generation UI allowed_actions 无效。")
    for action in allowed_actions:
        if not isinstance(action, dict):
            raise ValueError("Generation UI action 必须是对象。")
        if action.get("event_type") not in {"action", "submit", "confirm"}:
            raise ValueError("Generation UI action event_type 不受支持。")
        if not isinstance(action.get("id"), str) or not action["id"]:
            raise ValueError("Generation UI action id 不能为空。")
        schema = action.get("arguments_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("Generation UI action arguments_schema 无效。")
    normalized = deepcopy(contract)
    normalized["ui_capabilities"] = list(dict.fromkeys(capabilities))
    return normalized


def infer_data_schema(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """只描述可信数据的形状，不把值交给模型重写。"""
    if depth >= 5:
        return {}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {
                str(key): infer_data_schema(item, depth=depth + 1)
                for key, item in value.items()
            },
            "required": [str(key) for key in value],
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "items": infer_data_schema(value[0], depth=depth + 1) if value else {},
        }
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if value is None:
        return {"type": "null"}
    return {"type": "string"}


__all__ = [
    "INTERACTION_TYPES",
    "UI_CAPABILITIES",
    "default_generation_ui_contract",
    "structured_input_contract",
    "validate_generation_ui_contract",
]
