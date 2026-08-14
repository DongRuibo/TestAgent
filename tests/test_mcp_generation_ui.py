"""材料类 MCP 结果到 Generation UI 的受控映射测试。"""

from __future__ import annotations

from typing import Any

from tools.mcp_generation_ui import (
    McpMaterialGenerationUiAdapter,
    prepare_material_estimate_input,
    supports_material_estimate_input,
)
from tools.present_generation_ui import (
    GenerationUiPresenter,
    TrustedPresentationResultRegistry,
)


def _handler(tool: str, result: dict[str, Any]):
    def call(**_arguments: Any) -> dict[str, Any]:
        return {
            "success": True,
            "source": "mcp",
            "server": "lazytt",
            "tool": tool,
            "result": result,
        }

    return call


def test_material_estimate_input_uses_fixed_prefilled_form() -> None:
    registry = TrustedPresentationResultRegistry()

    prepared = prepare_material_estimate_input(registry)

    assert prepared["success"] is True
    presentation = prepared["presentation"]
    assert presentation["scenario"] == "structured_input"
    assert [field["name"] for field in presentation["data"]["fields"]] == [
        "paint_area_m2",
        "floor_area_m2",
        "room_perimeter_m",
        "door_total_width_m",
        "paint_coverage_m2_per_l",
        "paint_package_sizes_l",
        "tile_size_mm",
        "tiles_per_box",
        "flooring_package_coverage_m2",
        "confirm_standard_assumptions",
    ]

    presenter = GenerationUiPresenter(registry)
    rendered = presenter.present(
        "structured_input",
        "材料采购估算",
        presentation["result_ref"],
    )

    assert rendered["success"] is True
    defaults = presenter.artifacts[0]["spec"]["form_defaults"]
    field_nodes = presenter.artifacts[0]["spec"]["root"]["children"][0][
        "children"
    ][0]["children"]
    assert defaults["paint_area_m2"] is None
    assert defaults["paint_coverage_m2_per_l"] == 10
    assert defaults["paint_package_sizes_l"] == "1,5,18"
    assert defaults["tile_size_mm"] == "600×600 mm"
    assert defaults["flooring_package_coverage_m2"] == 2.2
    assert defaults["confirm_standard_assumptions"] is False
    assert field_nodes[0]["props"]["step"] == 0.01
    assert field_nodes[0]["props"]["maxFractionDigits"] == 2
    assert field_nodes[3]["props"]["step"] == 0.01
    assert field_nodes[7]["props"]["step"] == 1
    assert field_nodes[8]["props"]["step"] == 0.1


def test_material_estimate_input_requires_all_three_remote_calculators() -> None:
    assert supports_material_estimate_input(
        [
            "mcp__lazytt__paint_calculate",
            "mcp__lazytt__tile_calculate",
            "mcp__lazytt__flooring_calculate",
        ]
    )
    assert not supports_material_estimate_input(
        ["mcp__lazytt__paint_calculate", "mcp__lazytt__tile_calculate"]
    )


def test_material_mcp_results_build_one_trusted_generation_ui() -> None:
    registry = TrustedPresentationResultRegistry()
    adapter = McpMaterialGenerationUiAdapter(registry)
    handlers = adapter.wrap_handlers(
        {
            "mcp__lazytt__area_calculate": _handler(
                "area_calculate",
                {
                    "summary": "面积已计算。",
                    "rooms": [
                        {
                            "name": "卧室",
                            "floorAreaM2": 20,
                            "ceilingAreaM2": 20,
                            "netWallAreaM2": 50.4,
                        }
                    ],
                    "totals": {"floorAreaM2": 20, "netWallAreaM2": 50.4},
                },
            ),
            "mcp__lazytt__paint_calculate": _handler(
                "paint_calculate",
                {
                    "summary": "油漆已计算。",
                    "primer": {
                        "packages": [{"sizeL": 5, "quantity": 1}],
                        "purchasedLiters": 5,
                        "surplusLiters": 0.5,
                        "theoreticalLiters": 4,
                        "withWasteLiters": 4.5,
                    },
                    "finish": {
                        "packages": [{"sizeL": 5, "quantity": 2}],
                        "purchasedLiters": 10,
                        "surplusLiters": 1,
                        "theoreticalLiters": 8,
                        "withWasteLiters": 9,
                    },
                },
            ),
            "mcp__lazytt__tile_calculate": _handler(
                "tile_calculate",
                {
                    "summary": "瓷砖已计算。",
                    "netTiles": 56,
                    "tilesWithWaste": 62,
                    "purchasedTiles": 64,
                    "requiredBoxes": 16,
                    "surplusTiles": 2,
                },
            ),
            "mcp__lazytt__flooring_calculate": _handler(
                "flooring_calculate",
                {
                    "summary": "地板已计算。",
                    "requiredFlooringM2": 21.6,
                    "requiredPackages": 10,
                    "purchasedCoverageM2": 22,
                    "surplusCoverageM2": 0.4,
                    "netSkirtingLengthM": 18,
                    "requiredSkirtingLengthM": 18.9,
                    "requiredSkirtingPieces": 8,
                },
            ),
        }
    )

    handlers["mcp__lazytt__area_calculate"](
        rooms=[],
        deductDoors=True,
        deductWindows=True,
    )
    handlers["mcp__lazytt__paint_calculate"](
        primerCoats=1,
        finishCoats=2,
        wasteRate=0.1,
    )
    handlers["mcp__lazytt__tile_calculate"](
        tileLengthMm=600,
        tileWidthMm=600,
        layingPattern="straight",
        wasteRate=0.1,
    )
    final = handlers["mcp__lazytt__flooring_calculate"](
        packageCoverageM2=2.2,
        wasteRate=0.08,
        skirtingWasteRate=0.05,
    )

    presentation = final["presentation"]
    assert presentation["scenario"] == "mcp_material_estimate"
    assert presentation["data"]["metrics"] == {
        "net_wall_area_m2": 50.4,
        "floor_area_m2": 20,
        "primer_liters": 5,
        "finish_liters": 10,
        "tile_boxes": 16,
        "flooring_packages": 10,
        "skirting_pieces": 8,
    }
    assert presentation["data"]["tools"] == [
        "area_calculate",
        "paint_calculate",
        "tile_calculate",
        "flooring_calculate",
    ]

    presenter = GenerationUiPresenter(registry)
    rendered = presenter.present(
        "mcp_material_estimate",
        "卧室材料估算",
        presentation["result_ref"],
    )

    assert rendered["success"] is True
    artifact = presenter.artifacts[0]
    assert artifact["spec"]["interaction_mode"] == "read_only"
    assert artifact["spec"]["actions"] == []
    assert artifact["data"]["details"][0]["item"] == "卧室"
    assert artifact["spec"]["root"]["children"][1]["children"][0]["type"] == "DataTable"


def test_failed_material_mcp_result_is_not_registered() -> None:
    registry = TrustedPresentationResultRegistry()
    adapter = McpMaterialGenerationUiAdapter(registry)
    handler = adapter.wrap_handlers(
        {
            "mcp__lazytt__paint_calculate": lambda **_arguments: {
                "success": False,
                "source": "mcp",
                "server": "lazytt",
                "tool": "paint_calculate",
                "error_type": "MCP_TOOL_ERROR",
            }
        }
    )["mcp__lazytt__paint_calculate"]

    result = handler(paintAreaM2=50)

    assert "presentation" not in result
