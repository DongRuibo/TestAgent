from __future__ import annotations

import json

from tools.present_generation_ui import (
    PREPARE_GENERATION_UI_INPUT_TOOL,
    PRESENT_GENERATION_UI_TOOL,
    GenerationUiPresenter,
    TrustedSalesResultRegistry,
    prepare_generation_ui_input,
)


def test_presenter_builds_sales_briefing_from_trusted_analysis_result() -> None:
    results = TrustedSalesResultRegistry()
    analysis = results.attach_analysis_result(
        "fixtures/sales.csv",
        {"success": True, "analysis": {"source_file": "sales.csv"}},
    )
    presenter = GenerationUiPresenter(results)

    presented = presenter.present(
        "sales_briefing",
        "销售经营简报",
        analysis["presentation"]["result_ref"],
    )

    assert presented["success"] is True
    assert analysis["presentation"]["data"]["metrics"]["sales"] == 800_000
    assert analysis["presentation"]["interaction_type"] == "visualize"
    assert analysis["presentation"]["ui_capabilities"] == [
        "metrics",
        "chart",
        "table",
    ]
    assert analysis["presentation"]["allowed_actions"] == []
    assert analysis["presentation"]["data_schema"]["type"] == "object"
    assert len(presenter.artifacts) == 1
    artifact = presenter.artifacts[0]
    assert artifact["type"] == "generation_ui"
    assert artifact["spec"]["interaction_mode"] == "read_only"
    assert artifact["spec"]["actions"] == []
    assert artifact["data"]["metrics"]["sales"] == 800_000
    assert artifact["data"]["trend"] == [
        {"label": "2026-01", "value": 200_000},
        {"label": "2026-02", "value": 230_000},
        {"label": "2026-03", "value": 370_000},
    ]
    assert artifact["data"]["breakdown"] == [
        {"name": "华东", "sales": 465_000},
        {"name": "华北", "sales": 95_000},
        {"name": "华南", "sales": 240_000},
    ]
    assert "order_count" not in json.dumps(artifact, ensure_ascii=False)
    table = artifact["spec"]["root"]["children"][2]["children"][0]
    assert [column["field"] for column in table["props"]["columns"]] == [
        "name",
        "sales",
    ]


def test_presenter_rejects_forged_or_previous_run_result_ref() -> None:
    first_run = TrustedSalesResultRegistry()
    analysis = first_run.attach_analysis_result(
        "fixtures/sales.csv",
        {"success": True},
    )
    second_run_presenter = GenerationUiPresenter(TrustedSalesResultRegistry())

    result = second_run_presenter.present(
        "sales_briefing",
        "销售简报",
        analysis["presentation"]["result_ref"],
    )

    assert result["success"] is False
    assert result["error_type"] == "UNTRUSTED_GENERATION_UI_RESULT"
    assert second_run_presenter.artifacts == []


def test_only_successful_supported_analysis_gets_result_ref() -> None:
    results = TrustedSalesResultRegistry()

    failed = results.attach_analysis_result(
        "fixtures/sales.csv",
        {"success": False, "error": "failed"},
    )
    unsupported = results.attach_analysis_result(
        "fixtures/operations.csv",
        {"success": True},
    )

    assert "presentation" not in failed
    assert "presentation" not in unsupported


def test_generation_ui_tool_accepts_reference_not_model_supplied_data() -> None:
    parameters = PRESENT_GENERATION_UI_TOOL["function"]["parameters"]

    assert parameters["required"] == ["scenario", "title", "result_ref"]
    assert "data" not in parameters["properties"]
    assert "summary" not in parameters["properties"]


def test_structured_input_uses_safe_fields_and_trusted_result_ref() -> None:
    results = TrustedSalesResultRegistry()
    prepared = prepare_generation_ui_input(
        results,
        "请一次填写行程条件。",
        [
            {"name": "departure", "label": "出发地", "type": "text"},
            {"name": "travel_date", "label": "出发日期", "type": "date"},
            {
                "name": "cabin",
                "label": "舱位",
                "type": "select",
                "options": ["经济舱", "公务舱"],
            },
        ],
        "提交条件",
    )

    presentation = prepared["presentation"]
    assert presentation["interaction_type"] == "collect_input"
    assert presentation["ui_capabilities"] == ["form"]
    assert presentation["allowed_actions"][0]["event_type"] == "submit"
    presenter = GenerationUiPresenter(results)
    result = presenter.present(
        "structured_input",
        "填写行程条件",
        presentation["result_ref"],
    )

    assert result["success"] is True
    artifact = presenter.artifacts[0]
    assert artifact["spec"]["interaction_mode"] == "host_action"
    assert artifact["spec"]["actions"][0]["id"] == "submit_structured_input"
    assert artifact["spec"]["actions"][0]["arguments_schema"]["required"] == [
        "departure",
        "travel_date",
        "cabin",
    ]
    form = artifact["spec"]["root"]["children"][0]["children"][0]
    assert [node["type"] for node in form["children"]] == [
        "TextInput",
        "DatePicker",
        "Select",
        "ActionButton",
    ]


def test_structured_input_rejects_too_few_or_unsafe_fields() -> None:
    results = TrustedSalesResultRegistry()

    too_few = prepare_generation_ui_input(
        results,
        "缺少条件",
        [
            {"name": "one", "label": "一", "type": "text"},
            {"name": "two", "label": "二", "type": "text"},
        ],
    )
    unsafe = prepare_generation_ui_input(
        results,
        "不安全字段",
        [
            {"name": "one", "label": "一", "type": "password"},
            {"name": "two", "label": "二", "type": "text"},
            {"name": "three", "label": "三", "type": "text"},
        ],
    )

    assert too_few["error_type"] == "INVALID_GENERATION_UI_INPUT"
    assert unsafe["error_type"] == "INVALID_GENERATION_UI_INPUT"
    parameters = PREPARE_GENERATION_UI_INPUT_TOOL["function"]["parameters"]
    assert parameters["properties"]["fields"]["minItems"] == 3
    assert "password" not in parameters["properties"]["fields"]["items"]["properties"]["type"]["enum"]


def test_structured_input_validates_and_renders_decimal_step() -> None:
    results = TrustedSalesResultRegistry()
    prepared = prepare_generation_ui_input(
        results,
        "请填写尺寸。",
        [
            {"name": "width", "label": "宽度", "type": "number", "step": 0.01},
            {"name": "coverage", "label": "覆盖率", "type": "number", "step": 0.1},
            {"name": "count", "label": "数量", "type": "number", "step": 1},
        ],
    )
    presenter = GenerationUiPresenter(results)

    result = presenter.present(
        "structured_input",
        "填写尺寸",
        prepared["presentation"]["result_ref"],
    )

    assert result["success"] is True
    nodes = presenter.artifacts[0]["spec"]["root"]["children"][0]["children"][0][
        "children"
    ]
    assert nodes[0]["props"]["step"] == 0.01
    assert nodes[0]["props"]["maxFractionDigits"] == 2
    assert nodes[1]["props"]["maxFractionDigits"] == 1
    assert nodes[2]["props"]["maxFractionDigits"] == 0

    invalid = prepare_generation_ui_input(
        results,
        "无效步长。",
        [
            {"name": "one", "label": "一", "type": "number", "step": 0},
            {"name": "two", "label": "二", "type": "number"},
            {"name": "three", "label": "三", "type": "number"},
        ],
    )
    assert invalid["error_type"] == "INVALID_GENERATION_UI_INPUT"
