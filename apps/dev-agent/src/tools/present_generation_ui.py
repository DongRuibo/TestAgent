"""Agent 工具：把本轮可信分析结果转换为只读 Generation UI。"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from tools.direct_spreadsheet import _resolve_spreadsheet
from tools.content_operations_ui import (
    CONTENT_UI_SCENARIOS,
    build_content_operations_ui_artifact,
)
from tools.generation_ui_contract import (
    default_generation_ui_contract,
    structured_input_contract,
    validate_generation_ui_contract,
)
from tools.owner_briefing_ui import build_owner_briefing_ui_artifact
from tools.renovation_ui import build_renovation_ui_artifact


SUPPORTED_GENERATION_UI_SCENARIOS = (
    "sales_briefing",
    "owner_daily_briefing",
    "content_weekly_plan",
    "content_performance_review",
    "renovation_project_create",
    "material_quantity",
    "mcp_material_estimate",
    "quote_comparison",
    "construction_schedule",
    "contract_review",
    "structured_input",
)

_SCENARIO_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


PREPARE_GENERATION_UI_INPUT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "prepare_generation_ui_input",
        "description": (
            "当完成任务前需要用户一次填写至少 3 个结构化参数时，准备一个受控表单。"
            "简单问答、单个问题或业务工具已经提供 result_ref 时不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "说明为什么需要这些信息，最多 240 个字符。",
                },
                "fields": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "pattern": "^[a-z][a-z0-9_]{0,39}$",
                            },
                            "label": {"type": "string", "maxLength": 40},
                            "type": {
                                "type": "string",
                                "enum": [
                                    "text",
                                    "textarea",
                                    "number",
                                    "date",
                                    "select",
                                    "boolean",
                                ],
                            },
                            "required": {"type": "boolean"},
                            "placeholder": {"type": "string", "maxLength": 100},
                            "options": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 20,
                                "items": {"type": "string", "maxLength": 60},
                            },
                            "min": {"type": "number"},
                            "max": {"type": "number"},
                            "step": {"type": "number", "exclusiveMinimum": 0},
                        },
                        "required": ["name", "label", "type"],
                        "additionalProperties": False,
                    },
                },
                "submit_label": {"type": "string", "maxLength": 30},
            },
            "required": ["summary", "fields"],
            "additionalProperties": False,
        },
    },
}


PRESENT_GENERATION_UI_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "present_generation_ui",
        "description": (
            "把本轮业务工具返回的可信 result_ref 展示为受控 Generation UI。"
            "必须先调用对应业务工具并原样使用其返回的 presentation.result_ref。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scenario": {
                    "type": "string",
                    "enum": list(SUPPORTED_GENERATION_UI_SCENARIOS),
                    "description": "必须与业务工具返回的 presentation.scenario 一致。",
                },
                "title": {
                    "type": "string",
                    "description": "界面标题，最多 80 个字符。",
                },
                "result_ref": {
                    "type": "string",
                    "description": "本轮表格分析结果中 presentation.result_ref 的原值。",
                },
            },
            "required": ["scenario", "title", "result_ref"],
            "additionalProperties": False,
        },
    },
}


class UntrustedResultReference(ValueError):
    """result_ref 不属于当前 Agent 运行。"""


class TrustedPresentationResultRegistry:
    """只保存当前 Agent 运行中成功业务工具产生的 ViewModel。"""

    def __init__(self) -> None:
        self._results: dict[
            str,
            tuple[str, dict[str, Any], dict[str, Any]],
        ] = {}

    def register(
        self,
        scenario: str,
        data: dict[str, Any],
        *,
        contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(scenario, str) or not _SCENARIO_PATTERN.fullmatch(scenario):
            raise ValueError("Generation UI scenario 格式无效。")
        if not isinstance(data, dict):
            raise ValueError("可信展示数据必须是对象。")
        if contract is None:
            if scenario not in SUPPORTED_GENERATION_UI_SCENARIOS:
                raise ValueError(f"场景 {scenario} 必须提供通用交互契约。")
            safe_contract = default_generation_ui_contract(scenario, data)
        else:
            safe_contract = validate_generation_ui_contract(contract)
        result_ref = f"{scenario}-{uuid4()}"
        stored = deepcopy(data)
        self._results[result_ref] = (scenario, stored, deepcopy(safe_contract))
        return {
            "scenario": scenario,
            "result_ref": result_ref,
            "data": deepcopy(stored),
            **deepcopy(safe_contract),
        }

    def attach_analysis_result(
        self,
        path: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        response = deepcopy(result)
        if result.get("success") is not True:
            return response
        try:
            data = build_sales_briefing_view_model(path)
        except (OSError, UnicodeError, csv.Error, ValueError):
            return response

        response["presentation"] = self.register("sales_briefing", data)
        return response

    def resolve(self, scenario: str, result_ref: str) -> dict[str, Any]:
        data, _contract = self.resolve_presentation(scenario, result_ref)
        return data

    def resolve_presentation(
        self,
        scenario: str,
        result_ref: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(scenario, str) or not _SCENARIO_PATTERN.fullmatch(scenario):
            raise ValueError("Generation UI scenario 格式无效。")
        if not isinstance(result_ref, str) or not result_ref.strip():
            raise UntrustedResultReference("result_ref 必须是本轮分析返回的非空引用。")
        entry = self._results.get(result_ref.strip())
        if entry is None:
            raise UntrustedResultReference("result_ref 不属于本轮成功的业务结果。")
        stored_scenario, data, contract = entry
        if stored_scenario != scenario:
            raise UntrustedResultReference("result_ref 与请求的场景不匹配。")
        return deepcopy(data), deepcopy(contract)


# 兼容已经接入 P1 销售场景的调用方。
TrustedSalesResultRegistry = TrustedPresentationResultRegistry


class GenerationUiPresenter:
    """按一次 Agent 运行收集已经通过校验的 UI Artifact。"""

    def __init__(
        self, results: TrustedPresentationResultRegistry | None = None
    ) -> None:
        self.results = results or TrustedPresentationResultRegistry()
        self.artifacts: list[dict[str, Any]] = []

    def present(
        self,
        scenario: str,
        title: str,
        result_ref: str,
    ) -> dict[str, Any]:
        try:
            data, contract = self.results.resolve_presentation(scenario, result_ref)
            artifact = (
                _build_sales_briefing(scenario, title, data)
                if scenario == "sales_briefing"
                else build_content_operations_ui_artifact(
                    scenario, title, data, contract
                )
                if scenario in CONTENT_UI_SCENARIOS
                else build_owner_briefing_ui_artifact(title, data, contract)
                if scenario == "owner_daily_briefing"
                else _build_structured_input(title, data, contract)
                if scenario == "structured_input"
                else build_renovation_ui_artifact(scenario, title, data)
            )
        except UntrustedResultReference as exc:
            return {
                "success": False,
                "error_type": "UNTRUSTED_GENERATION_UI_RESULT",
                "message": str(exc),
                "retryable": False,
            }
        except ValueError as exc:
            return {
                "success": False,
                "error_type": "INVALID_GENERATION_UI_ARGUMENTS",
                "message": str(exc),
                "retryable": False,
            }
        self.artifacts.append(artifact)
        return {
            "success": True,
            "artifact_id": artifact["artifact_id"],
            "scenario": artifact["scenario"],
            "message": "可信业务结果已生成 Generation UI；请继续给出简短文字结论。",
        }


def prepare_generation_ui_input(
    registry: TrustedPresentationResultRegistry,
    summary: str,
    fields: list[dict[str, Any]],
    submit_label: str = "提交并继续",
) -> dict[str, Any]:
    """把模型提出的问题约束成安全表单，而不是接受任意 UISpec。"""
    try:
        safe_summary = _text(summary, "summary", max_chars=240)
        safe_submit_label = _text(submit_label, "submit_label", max_chars=30)
        safe_fields = _validate_input_fields(fields)
        contract = structured_input_contract(safe_fields)
    except ValueError as exc:
        return {
            "success": False,
            "error_type": "INVALID_GENERATION_UI_INPUT",
            "message": str(exc),
            "retryable": False,
        }
    data = {
        "status": "待填写",
        "summary": safe_summary,
        "fields": safe_fields,
        "submit_label": safe_submit_label,
    }
    return {
        "success": True,
        "presentation": registry.register(
            "structured_input",
            data,
            contract=contract,
        ),
    }


def _build_structured_input(
    title: str,
    data: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    safe_title = _text(title, "title", max_chars=80)
    if contract.get("interaction_type") != "collect_input":
        raise ValueError("structured_input 必须使用 collect_input 契约。")
    fields = _validate_input_fields(data.get("fields"))
    action_contract = next(
        (
            item
            for item in contract["allowed_actions"]
            if item.get("id") == "submit_structured_input"
        ),
        None,
    )
    if action_contract is None:
        raise ValueError("structured_input 缺少提交 Action。")
    field_nodes = [_input_field_node(index, field) for index, field in enumerate(fields)]
    field_nodes.append(
        {
            "id": "submit-structured-input",
            "type": "ActionButton",
            "props": {
                "actionId": "submit_structured_input",
                "label": data["submit_label"],
                "tone": "primary",
            },
            "children": [],
        }
    )
    form_defaults = {
        field["name"]: _input_default(field)
        for field in fields
    }
    spec = {
        "version": "0.1",
        "scenario": "structured_input",
        "interaction_mode": "host_action",
        "title": safe_title,
        "summary": data["summary"],
        "root": {
            "id": "root",
            "type": "Column",
            "props": {},
            "children": [
                {
                    "id": "structured-input-card",
                    "type": "Card",
                    "props": {"title": "请补充信息"},
                    "children": [
                        {
                            "id": "structured-input-form",
                            "type": "Form",
                            "props": {"title": data["summary"]},
                            "children": field_nodes,
                        }
                    ],
                }
            ],
        },
        "actions": [
            {
                "id": "submit_structured_input",
                "label": data["submit_label"],
                "tool": "resume_agent_with_input",
                "event_type": "submit",
                "confirm": False,
                "tone": "primary",
                "arguments_schema": deepcopy(action_contract["arguments_schema"]),
            }
        ],
        "form_defaults": form_defaults,
    }
    return {
        "type": "generation_ui",
        "artifact_id": str(uuid4()),
        "scenario": "structured_input",
        "text": data["summary"],
        "spec": spec,
        "data": deepcopy(data),
    }


def _validate_input_fields(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 3 <= len(value) <= 10:
        raise ValueError("结构化表单需要 3-10 个字段。")
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, field in enumerate(value, start=1):
        if not isinstance(field, dict):
            raise ValueError(f"第 {index} 个表单字段必须是对象。")
        allowed = {
            "name",
            "label",
            "type",
            "required",
            "placeholder",
            "options",
            "min",
            "max",
            "step",
            "default",
        }
        extra = set(field) - allowed
        if extra:
            raise ValueError(f"第 {index} 个表单字段包含不支持的属性。")
        name = field.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
            raise ValueError(f"第 {index} 个表单字段 name 格式无效。")
        if name in names:
            raise ValueError("表单字段 name 不能重复。")
        names.add(name)
        label = _text(field.get("label"), f"第 {index} 个字段 label", 40)
        field_type = field.get("type")
        if field_type not in {"text", "textarea", "number", "date", "select", "boolean"}:
            raise ValueError(f"第 {index} 个表单字段 type 不受支持。")
        item: dict[str, Any] = {
            "name": name,
            "label": label,
            "type": field_type,
            "required": field.get("required", True) is not False,
        }
        placeholder = field.get("placeholder")
        if placeholder is not None:
            item["placeholder"] = _text(
                placeholder,
                f"第 {index} 个字段 placeholder",
                100,
            )
        if field_type == "select":
            options = field.get("options")
            if (
                not isinstance(options, list)
                or not 1 <= len(options) <= 20
                or any(not isinstance(option, str) or not option.strip() for option in options)
            ):
                raise ValueError(f"第 {index} 个选择字段必须提供 1-20 个 options。")
            safe_options = [_text(option, "option", 60) for option in options]
            if len(set(safe_options)) != len(safe_options):
                raise ValueError(f"第 {index} 个选择字段 options 不能重复。")
            item["options"] = safe_options
        elif "options" in field:
            raise ValueError("只有 select 字段可以包含 options。")
        if field_type == "number":
            for bound in ("min", "max", "step"):
                if bound in field:
                    number = field[bound]
                    if isinstance(number, bool) or not isinstance(number, (int, float)):
                        raise ValueError(f"第 {index} 个字段 {bound} 必须是数字。")
                    item[bound] = number
            if item.get("min") is not None and item.get("max") is not None and item["min"] > item["max"]:
                raise ValueError(f"第 {index} 个字段 min 不能大于 max。")
            if item.get("step", 1) <= 0:
                raise ValueError(f"第 {index} 个字段 step 必须大于 0。")
        elif "min" in field or "max" in field or "step" in field:
            raise ValueError("只有 number 字段可以包含 min/max/step。")
        if "default" in field:
            default = field["default"]
            if field_type == "number":
                if isinstance(default, bool) or not isinstance(default, (int, float)):
                    raise ValueError(f"第 {index} 个数字字段 default 必须是数字。")
                if "min" in item and default < item["min"]:
                    raise ValueError(f"第 {index} 个数字字段 default 不能小于 min。")
                if "max" in item and default > item["max"]:
                    raise ValueError(f"第 {index} 个数字字段 default 不能大于 max。")
            elif field_type == "boolean":
                if not isinstance(default, bool):
                    raise ValueError(f"第 {index} 个布尔字段 default 必须是布尔值。")
            elif not isinstance(default, str):
                raise ValueError(f"第 {index} 个文本字段 default 必须是字符串。")
            elif field_type == "select" and default not in item["options"]:
                raise ValueError(f"第 {index} 个选择字段 default 必须来自 options。")
            item["default"] = deepcopy(default)
        normalized.append(item)
    return normalized


def _input_field_node(index: int, field: dict[str, Any]) -> dict[str, Any]:
    component_types = {
        "text": "TextInput",
        "textarea": "TextArea",
        "number": "NumberInput",
        "date": "DatePicker",
        "select": "Select",
        "boolean": "Checkbox",
    }
    props: dict[str, Any] = {
        "label": field["label"],
        "model": field["name"],
    }
    for key in ("placeholder", "options", "min", "max", "step"):
        if key in field:
            props[key] = deepcopy(field[key])
    if field["type"] == "number" and "step" in field:
        props["maxFractionDigits"] = _fraction_digits(field["step"])
    return {
        "id": f"input-{index}-{field['name']}",
        "type": component_types[field["type"]],
        "props": props,
        "children": [],
    }


def _input_default(field: dict[str, Any]) -> Any:
    if "default" in field:
        return deepcopy(field["default"])
    if field["type"] == "boolean":
        return False
    return None if field["type"] == "number" else ""


def _fraction_digits(step: int | float) -> int:
    return max(0, -Decimal(str(step)).as_tuple().exponent)


def build_sales_briefing_view_model(path: str) -> dict[str, Any]:
    """从受控目录内的销售汇总 CSV 构造确定性 ViewModel。"""
    source = _resolve_spreadsheet(path)
    if source.suffix.lower() != ".csv":
        raise ValueError("sales_briefing 当前只支持 CSV。")
    if not source.exists() or not source.is_file():
        raise ValueError(f"销售汇总不存在：{path}")

    monthly_sales: dict[str, Decimal] = defaultdict(Decimal)
    regional_sales: dict[str, Decimal] = defaultdict(Decimal)
    regional_order_ids: dict[str, set[str]] = defaultdict(set)
    order_ids: set[str] = set()
    row_count = 0

    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            raise ValueError("销售汇总 CSV 包含重复列名。")
        required = {"month", "region", "product", "amount"}
        missing = sorted(required - set(fields))
        if missing:
            raise ValueError(f"销售汇总 CSV 缺少列：{', '.join(missing)}。")
        has_order_id = "order_id" in fields

        for row_count, row in enumerate(reader, start=1):
            if row_count > 10_000:
                raise ValueError("销售汇总 CSV 最多支持 10000 行。")
            month = _csv_text(row.get("month"), f"第 {row_count} 行 month")
            region = _csv_text(row.get("region"), f"第 {row_count} 行 region")
            _csv_text(row.get("product"), f"第 {row_count} 行 product")
            amount = _csv_amount(row.get("amount"), f"第 {row_count} 行 amount")
            monthly_sales[month] += amount
            regional_sales[region] += amount
            if has_order_id:
                order_id = _csv_text(
                    row.get("order_id"), f"第 {row_count} 行 order_id"
                )
                order_ids.add(order_id)
                regional_order_ids[region].add(order_id)

    if row_count == 0:
        raise ValueError("销售汇总 CSV 不能为空。")
    if len(monthly_sales) > 60:
        raise ValueError("sales_briefing 最多支持 60 个月。")
    if len(regional_sales) > 50:
        raise ValueError("sales_briefing 最多支持 50 个区域。")

    trend = [
        {"label": month, "value": _json_number(monthly_sales[month])}
        for month in sorted(monthly_sales)
    ]
    metrics: dict[str, int | float] = {
        "sales": _json_number(sum(monthly_sales.values(), Decimal())),
    }
    if order_ids:
        metrics["order_count"] = len(order_ids)
    if len(trend) >= 2 and trend[-2]["value"] != 0:
        previous = float(trend[-2]["value"])
        current = float(trend[-1]["value"])
        metrics["growth_rate"] = (current - previous) / previous

    breakdown = []
    for region in sorted(regional_sales):
        item: dict[str, Any] = {
            "name": region,
            "sales": _json_number(regional_sales[region]),
        }
        if order_ids:
            item["order_count"] = len(regional_order_ids[region])
        breakdown.append(item)

    return {
        "source_file": source.name,
        "row_count": row_count,
        "metrics": metrics,
        "trend": trend,
        "breakdown": breakdown,
    }


def _build_sales_briefing(
    scenario: str,
    title: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    if scenario != "sales_briefing":
        raise ValueError("当前只支持 sales_briefing 场景。")
    safe_title = _text(title, "title", max_chars=80)
    summary = (
        f"已根据 {data['source_file']} 汇总 {data['row_count']} 行销售数据，"
        f"覆盖 {len(data['trend'])} 个月和 {len(data['breakdown'])} 个区域。"
    )
    metrics = data["metrics"]
    metric_nodes = [_metric("sales", "销售额", "metrics.sales", "currency")]
    if "order_count" in metrics:
        metric_nodes.append(_metric("orders", "订单数", "metrics.order_count"))
    if "growth_rate" in metrics:
        metric_nodes.append(
            _metric(
                "growth",
                "环比增长",
                "metrics.growth_rate",
                "percent",
                tone="success" if metrics["growth_rate"] >= 0 else "danger",
            )
        )

    children: list[dict[str, Any]] = [
        {
            "id": "metrics-card",
            "type": "Card",
            "props": {"title": "核心指标"},
            "children": [
                {
                    "id": "metrics",
                    "type": "MetricGrid",
                    "props": {},
                    "children": metric_nodes,
                }
            ],
        }
    ]
    if data["trend"]:
        children.append(
            {
                "id": "trend-card",
                "type": "Card",
                "props": {"title": "销售趋势"},
                "children": [
                    {
                        "id": "sales-trend",
                        "type": "Chart",
                        "props": {
                            "path": "trend",
                            "type": "line",
                            "labelField": "label",
                            "valueField": "value",
                        },
                        "children": [],
                    }
                ],
            }
        )
    if data["breakdown"]:
        columns = [
            {"field": "name", "header": "区域"},
            {"field": "sales", "header": "销售额", "format": "currency"},
        ]
        if "order_count" in metrics:
            columns.append({"field": "order_count", "header": "订单数"})
        children.append(
            {
                "id": "breakdown-card",
                "type": "Card",
                "props": {"title": "区域汇总"},
                "children": [
                    {
                        "id": "sales-breakdown",
                        "type": "DataTable",
                        "props": {"path": "breakdown", "columns": columns},
                        "children": [],
                    }
                ],
            }
        )

    return {
        "type": "generation_ui",
        "artifact_id": str(uuid4()),
        "scenario": scenario,
        "text": summary,
        "spec": {
            "version": "0.1",
            "scenario": scenario,
            "interaction_mode": "read_only",
            "title": safe_title,
            "summary": summary,
            "root": {
                "id": "root",
                "type": "Column",
                "props": {},
                "children": children,
            },
            "actions": [],
            "form_defaults": {},
        },
        "data": deepcopy(data),
    }


def _metric(
    node_id: str,
    label: str,
    path: str,
    value_format: str | None = None,
    *,
    tone: str = "default",
) -> dict[str, Any]:
    props: dict[str, Any] = {"label": label, "path": path, "tone": tone}
    if value_format is not None:
        props["format"] = value_format
    return {"id": node_id, "type": "Metric", "props": props, "children": []}


def _csv_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空文本。")
    return value.strip()


def _csv_amount(value: Any, field: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非负数字。")
    try:
        amount = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{field} 必须是非负数字。") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field} 必须是非负有限数字。")
    return amount


def _json_number(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


def _text(value: Any, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串。")
    normalized = value.strip()
    if len(normalized) > max_chars:
        raise ValueError(f"{field} 最多包含 {max_chars} 个字符。")
    return normalized
