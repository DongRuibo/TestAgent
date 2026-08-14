"""把成功的材料类 MCP 结果映射为本轮可信 Generation UI 数据。"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from typing import Any, Protocol

from tools.present_generation_ui import (
    TrustedPresentationResultRegistry,
    prepare_generation_ui_input,
)


MATERIAL_MCP_TOOLS = {
    "area_calculate",
    "paint_calculate",
    "tile_calculate",
    "flooring_calculate",
}
MATERIAL_ESTIMATE_REQUIRED_MCP_TOOLS = {
    "paint_calculate",
    "tile_calculate",
    "flooring_calculate",
}
PREPARE_MATERIAL_ESTIMATE_INPUT_TOOL_NAME = "prepare_material_estimate_input"
PREPARE_MATERIAL_ESTIMATE_INPUT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PREPARE_MATERIAL_ESTIMATE_INPUT_TOOL_NAME,
        "description": (
            "当用户要同时计算油漆、瓷砖和地板采购量，但没有提供完整参数时，"
            "准备固定且可预填的材料估算表单。不要改用通用表单或要求用户手写 JSON。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


class PresentationRegistry(Protocol):
    def register(self, scenario: str, data: dict[str, Any]) -> dict[str, Any]: ...


ToolHandler = Callable[..., dict[str, Any]]


def supports_material_estimate_input(tool_names: Iterable[str]) -> bool:
    remote_names = {_remote_tool_name(name) for name in tool_names}
    return MATERIAL_ESTIMATE_REQUIRED_MCP_TOOLS <= remote_names


def prepare_material_estimate_input(
    registry: TrustedPresentationResultRegistry,
) -> dict[str, Any]:
    """生成固定材料参数表单，避免模型临时拼出超限字段。"""
    return prepare_generation_ui_input(
        registry,
        (
            "请填写实际面积和周长。勾选确认表示采用：底漆1遍、面漆2遍，"
            "油漆/瓷砖损耗10%、瓷砖直铺、地板损耗8%，"
            "踢脚线损耗5%且每根2.4m。"
        ),
        [
            {
                "name": "paint_area_m2",
                "label": "油漆净涂刷面积（㎡）",
                "type": "number",
                "min": 0.1,
                "step": 0.01,
                "placeholder": "请输入实际净涂刷面积",
            },
            {
                "name": "floor_area_m2",
                "label": "瓷砖/地板铺装面积（㎡）",
                "type": "number",
                "min": 0.1,
                "step": 0.01,
                "placeholder": "请输入实际地面面积",
            },
            {
                "name": "room_perimeter_m",
                "label": "房间周长（m）",
                "type": "number",
                "min": 0.1,
                "step": 0.01,
                "placeholder": "用于计算踢脚线",
            },
            {
                "name": "door_total_width_m",
                "label": "门洞总宽（m，无则填 0）",
                "type": "number",
                "min": 0,
                "step": 0.01,
                "placeholder": "多个门洞填写宽度合计",
            },
            {
                "name": "paint_coverage_m2_per_l",
                "label": "油漆覆盖率（㎡/L）",
                "type": "number",
                "min": 0.1,
                "step": 0.1,
                "default": 10,
            },
            {
                "name": "paint_package_sizes_l",
                "label": "油漆包装规格（L，逗号分隔）",
                "type": "text",
                "default": "1,5,18",
            },
            {
                "name": "tile_size_mm",
                "label": "瓷砖规格",
                "type": "select",
                "options": ["300×300 mm", "600×600 mm", "750×1500 mm"],
                "default": "600×600 mm",
            },
            {
                "name": "tiles_per_box",
                "label": "每箱瓷砖块数",
                "type": "number",
                "min": 1,
                "step": 1,
                "default": 4,
            },
            {
                "name": "flooring_package_coverage_m2",
                "label": "每包地板覆盖面积（㎡）",
                "type": "number",
                "min": 0.1,
                "step": 0.1,
                "default": 2.2,
            },
            {
                "name": "confirm_standard_assumptions",
                "label": "确认采用上述常用工艺参数",
                "type": "boolean",
                "default": False,
            },
        ],
        "确认参数并计算",
    )


class McpMaterialGenerationUiAdapter:
    """在单次 Agent 运行中累计材料计算结果，并附加可信 result_ref。"""

    def __init__(self, registry: PresentationRegistry) -> None:
        self.registry = registry
        self._calculations: dict[str, dict[str, Any]] = {}

    def wrap_handlers(
        self,
        handlers: Mapping[str, ToolHandler],
    ) -> dict[str, ToolHandler]:
        return {
            name: self._wrap(name, handler)
            if _remote_tool_name(name) in MATERIAL_MCP_TOOLS
            else handler
            for name, handler in handlers.items()
        }

    def _wrap(self, local_name: str, handler: ToolHandler) -> ToolHandler:
        remote_name = _remote_tool_name(local_name)

        def call(**arguments: Any) -> dict[str, Any]:
            original = handler(**arguments)
            response = deepcopy(original)
            if (
                response.get("success") is not True
                or response.get("source") != "mcp"
                or response.get("tool") != remote_name
                or not isinstance(response.get("result"), dict)
            ):
                return response
            self._calculations[remote_name] = {
                "arguments": deepcopy(arguments),
                "result": deepcopy(response["result"]),
            }
            try:
                view_model = build_mcp_material_view_model(self._calculations)
                response["presentation"] = self.registry.register(
                    "mcp_material_estimate",
                    view_model,
                )
            except (KeyError, TypeError, ValueError):
                pass
            return response

        return call


def build_mcp_material_view_model(
    calculations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    metrics: dict[str, int | float] = {}
    details: list[dict[str, str]] = []
    assumptions: list[dict[str, str]] = []
    notes: list[str] = []
    tools: list[str] = []

    for tool_name in (
        "area_calculate",
        "paint_calculate",
        "tile_calculate",
        "flooring_calculate",
    ):
        calculation = calculations.get(tool_name)
        if calculation is None:
            continue
        arguments = _object(calculation.get("arguments"), f"{tool_name}.arguments")
        result = _object(calculation.get("result"), f"{tool_name}.result")
        tools.append(tool_name)
        summary = result.get("summary")
        if isinstance(summary, str) and summary.strip():
            notes.append(summary.strip()[:500])
        if tool_name == "area_calculate":
            _add_area(metrics, details, assumptions, arguments, result)
        elif tool_name == "paint_calculate":
            _add_paint(metrics, details, assumptions, arguments, result)
        elif tool_name == "tile_calculate":
            _add_tile(metrics, details, assumptions, arguments, result)
        else:
            _add_flooring(metrics, details, assumptions, arguments, result)

    if not metrics or not details:
        raise ValueError("材料计算结果不足以生成界面。")
    return {
        "metrics": metrics,
        "details": details,
        "assumptions": assumptions,
        "notes": list(dict.fromkeys(notes)),
        "tools": tools,
    }


def _add_area(
    metrics: dict[str, int | float],
    details: list[dict[str, str]],
    assumptions: list[dict[str, str]],
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    totals = _object(result.get("totals"), "area_calculate.totals")
    metrics["net_wall_area_m2"] = _number(
        totals.get("netWallAreaM2"), "netWallAreaM2"
    )
    metrics["floor_area_m2"] = _number(totals.get("floorAreaM2"), "floorAreaM2")
    rooms = result.get("rooms")
    if not isinstance(rooms, list) or not rooms:
        raise ValueError("area_calculate.rooms 无效。")
    for room in rooms:
        item = _object(room, "area_calculate.rooms[]")
        details.append(
            {
                "category": "面积",
                "item": _text(item.get("name"), "room.name"),
                "result": (
                    f"净墙 {_format_number(item.get('netWallAreaM2'))} ㎡；"
                    f"地面 {_format_number(item.get('floorAreaM2'))} ㎡；"
                    f"顶面 {_format_number(item.get('ceilingAreaM2'))} ㎡"
                ),
            }
        )
    assumptions.extend(
        [
            {"item": "扣除门洞", "value": _yes_no(arguments.get("deductDoors", True))},
            {"item": "扣除窗洞", "value": _yes_no(arguments.get("deductWindows", True))},
        ]
    )


def _add_paint(
    metrics: dict[str, int | float],
    details: list[dict[str, str]],
    assumptions: list[dict[str, str]],
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    primer = _object(result.get("primer"), "paint_calculate.primer")
    finish = _object(result.get("finish"), "paint_calculate.finish")
    metrics["primer_liters"] = _number(primer.get("purchasedLiters"), "primer")
    metrics["finish_liters"] = _number(finish.get("purchasedLiters"), "finish")
    details.extend(
        [
            _paint_detail("底漆", primer),
            _paint_detail("面漆", finish),
        ]
    )
    assumptions.extend(
        [
            {"item": "底漆遍数", "value": str(arguments.get("primerCoats"))},
            {"item": "面漆遍数", "value": str(arguments.get("finishCoats"))},
            {"item": "油漆损耗率", "value": _percent(arguments.get("wasteRate"))},
        ]
    )


def _paint_detail(label: str, coating: dict[str, Any]) -> dict[str, str]:
    packages = coating.get("packages")
    if not isinstance(packages, list):
        raise ValueError("paint packages 无效。")
    package_text = " + ".join(
        f"{_format_number(_object(item, 'package').get('sizeL'))}L×"
        f"{int(_number(_object(item, 'package').get('quantity'), 'quantity'))}"
        for item in packages
    )
    return {
        "category": "油漆",
        "item": label,
        "result": (
            f"理论 {_format_number(coating.get('theoreticalLiters'))} L；"
            f"含损耗 {_format_number(coating.get('withWasteLiters'))} L；"
            f"采购 {_format_number(coating.get('purchasedLiters'))} L"
            f"（{package_text}）；余量 {_format_number(coating.get('surplusLiters'))} L"
        ),
    }


def _add_tile(
    metrics: dict[str, int | float],
    details: list[dict[str, str]],
    assumptions: list[dict[str, str]],
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    metrics["tile_boxes"] = _number(result.get("requiredBoxes"), "requiredBoxes")
    details.append(
        {
            "category": "瓷砖",
            "item": "采购数量",
            "result": (
                f"净 {_format_number(result.get('netTiles'))} 块；"
                f"含损耗 {_format_number(result.get('tilesWithWaste'))} 块；"
                f"采购 {_format_number(result.get('purchasedTiles'))} 块 / "
                f"{_format_number(result.get('requiredBoxes'))} 箱；"
                f"余 {_format_number(result.get('surplusTiles'))} 块"
            ),
        }
    )
    assumptions.extend(
        [
            {
                "item": "瓷砖规格",
                "value": f"{arguments.get('tileLengthMm')}×{arguments.get('tileWidthMm')} mm",
            },
            {"item": "铺贴方式", "value": _text(arguments.get("layingPattern"), "layingPattern")},
            {"item": "瓷砖损耗率", "value": _percent(arguments.get("wasteRate"))},
        ]
    )


def _add_flooring(
    metrics: dict[str, int | float],
    details: list[dict[str, str]],
    assumptions: list[dict[str, str]],
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    metrics["flooring_packages"] = _number(
        result.get("requiredPackages"), "requiredPackages"
    )
    metrics["skirting_pieces"] = _number(
        result.get("requiredSkirtingPieces"), "requiredSkirtingPieces"
    )
    details.extend(
        [
            {
                "category": "地板",
                "item": "采购数量",
                "result": (
                    f"需求 {_format_number(result.get('requiredFlooringM2'))} ㎡；"
                    f"采购 {_format_number(result.get('requiredPackages'))} 包，"
                    f"覆盖 {_format_number(result.get('purchasedCoverageM2'))} ㎡；"
                    f"余量 {_format_number(result.get('surplusCoverageM2'))} ㎡"
                ),
            },
            {
                "category": "地板",
                "item": "踢脚线",
                "result": (
                    f"净长 {_format_number(result.get('netSkirtingLengthM'))} m；"
                    f"含损耗 {_format_number(result.get('requiredSkirtingLengthM'))} m；"
                    f"采购 {_format_number(result.get('requiredSkirtingPieces'))} 根"
                ),
            },
        ]
    )
    assumptions.extend(
        [
            {"item": "每包覆盖", "value": f"{arguments.get('packageCoverageM2')} ㎡"},
            {"item": "地板损耗率", "value": _percent(arguments.get("wasteRate"))},
            {"item": "踢脚线损耗率", "value": _percent(arguments.get("skirtingWasteRate"))},
        ]
    )


def _remote_tool_name(local_name: str) -> str:
    return local_name.rsplit("__", maxsplit=1)[-1]


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象。")
    return value


def _number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字。")
    if not math.isfinite(float(value)):
        raise ValueError(f"{field} 必须是有限数字。")
    return value


def _format_number(value: Any) -> str:
    number = _number(value, "value")
    return f"{float(number):g}"


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空文本。")
    return value.strip()[:200]


def _percent(value: Any) -> str:
    return f"{float(_number(value, 'rate')):.0%}"


def _yes_no(value: Any) -> str:
    if not isinstance(value, bool):
        raise ValueError("布尔参数无效。")
    return "是" if value else "否"


__all__ = [
    "MATERIAL_MCP_TOOLS",
    "MATERIAL_ESTIMATE_REQUIRED_MCP_TOOLS",
    "McpMaterialGenerationUiAdapter",
    "PREPARE_MATERIAL_ESTIMATE_INPUT_TOOL",
    "PREPARE_MATERIAL_ESTIMATE_INPUT_TOOL_NAME",
    "build_mcp_material_view_model",
    "prepare_material_estimate_input",
    "supports_material_estimate_input",
]
