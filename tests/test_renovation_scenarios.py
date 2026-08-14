from __future__ import annotations

from datetime import date, timedelta

import httpx

from tools.present_generation_ui import (
    GenerationUiPresenter,
    TrustedPresentationResultRegistry,
)
from tools.renovation_scenarios import RenovationScenarioTools, WeatherForecastClient


class FakeWeather:
    def forecast(self, city, start, *, days):
        assert city == "杭州市"
        values = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            values.append(
                {
                    "date": day.isoformat(),
                    "weather_code": 61 if offset == 6 else 1,
                    "precipitation_probability_max": 80 if offset == 6 else 10,
                    "temperature_2m_max": 26,
                    "temperature_2m_min": 18,
                }
            )
        return {"success": True, "provider": "synthetic-weather", "days": values}


def _present(registry, result, title="装修业务结果"):
    presenter = GenerationUiPresenter(registry)
    presentation = result["presentation"]
    presented = presenter.present(
        presentation["scenario"],
        title,
        presentation["result_ref"],
    )
    assert presented["success"] is True
    return presenter.artifacts[0]


def test_project_draft_requires_host_confirmed_address() -> None:
    registry = TrustedPresentationResultRegistry()
    tools = RenovationScenarioTools(registry, weather_client=FakeWeather())

    result = tools.prepare_project("杭州市", 120, "现代简约")
    artifact = _present(registry, result)

    assert artifact["data"] == {
        "city": "杭州市",
        "area_m2": 120,
        "style": "现代简约",
        "status": "待确认地址",
    }
    assert artifact["spec"]["interaction_mode"] == "host_action"
    assert artifact["spec"]["actions"] == [
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
    ]


def test_material_quantities_are_deterministic_and_show_assumptions() -> None:
    registry = TrustedPresentationResultRegistry()
    tools = RenovationScenarioTools(registry, weather_client=FakeWeather())

    result = tools.calculate_materials(
        [
            {
                "name": "客厅",
                "length_m": 6,
                "width_m": 4,
                "height_m": 2.8,
                "door_area_m2": 2,
                "window_area_m2": 4,
            }
        ]
    )
    artifact = _present(registry, result)

    assert artifact["data"]["rooms"][0] == {
        "name": "客厅",
        "wall_area_m2": 50,
        "floor_area_m2": 24,
        "paint_liters": 10,
        "tile_area_m2": 26.4,
        "flooring_area_m2": 25.92,
    }
    assert artifact["data"]["assumptions"]["paint_coats"] == 2
    assert artifact["spec"]["interaction_mode"] == "read_only"


def test_quote_comparison_marks_missing_math_and_price_anomalies() -> None:
    registry = TrustedPresentationResultRegistry()
    tools = RenovationScenarioTools(registry, weather_client=FakeWeather())

    result = tools.compare_quotes("fixtures/renovation_quotes.csv")
    artifact = _present(registry, result)

    anomaly_types = {item["type"] for item in artifact["data"]["anomalies"]}
    assert {"缺项", "算术不一致", "单价偏离"} <= anomaly_types
    assert [item["company"] for item in artifact["data"]["companies"]] == [
        "丙公司",
        "乙公司",
        "甲公司",
    ]
    comparison = artifact["spec"]["root"]["children"][1]["children"][0]
    assert comparison["type"] == "ComparisonTable"


def test_schedule_uses_structured_weather_and_marks_delay() -> None:
    registry = TrustedPresentationResultRegistry()
    tools = RenovationScenarioTools(registry, weather_client=FakeWeather())

    result = tools.plan_schedule("杭州市", "2026-08-17")
    artifact = _present(registry, result)

    assert artifact["data"]["weather_status"] == "已核验"
    assert artifact["data"]["weather_provider"] == "synthetic-weather"
    assert any("顺延" in warning for warning in artifact["data"]["warnings"])
    assert artifact["spec"]["root"]["children"][1]["children"][0]["type"] == "Timeline"


def test_contract_review_keeps_source_evidence_and_disclaimer() -> None:
    registry = TrustedPresentationResultRegistry()

    def search(_query):
        return {
            "success": True,
            "candidates": [
                {
                    "document_name": "装修合同.pdf",
                    "pages": [3],
                    "content": "签约后支付80%预付款，后续增项由施工方口头通知。",
                },
                {
                    "document_name": "装修合同.pdf",
                    "pages": [8],
                    "content": "工程保修期为一年，发生争议提交甲方所在地法院。",
                },
            ],
        }

    tools = RenovationScenarioTools(
        registry,
        weather_client=FakeWeather(),
        contract_search=search,
    )
    result = tools.review_contract()
    artifact = _present(registry, result)

    assert artifact["data"]["source_count"] == 2
    assert any("第 3 页" in risk["source"] for risk in artifact["data"]["risks"])
    assert "不构成正式法律意见" in artifact["data"]["disclaimer"]


def test_contract_review_requires_current_attachment() -> None:
    registry = TrustedPresentationResultRegistry()
    tools = RenovationScenarioTools(registry, weather_client=FakeWeather())

    result = tools.review_contract()

    assert result["success"] is False
    assert result["error_type"] == "CONTRACT_ATTACHMENT_REQUIRED"


def test_weather_client_uses_fixed_structured_endpoints() -> None:
    requested_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/v1/search":
            assert request.url.params["name"] == "杭州市"
            return httpx.Response(
                200,
                json={"results": [{"latitude": 30.27, "longitude": 120.15}]},
            )
        assert request.url.path == "/v1/forecast"
        return httpx.Response(
            200,
            json={
                "daily": {
                    "time": ["2026-08-17"],
                    "weather_code": [1],
                    "precipitation_probability_max": [10],
                    "temperature_2m_max": [29],
                    "temperature_2m_min": [21],
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = WeatherForecastClient(http_client=client).forecast(
        "杭州市", date(2026, 8, 17), days=1
    )

    assert result == {
        "success": True,
        "provider": "Open-Meteo",
        "days": [
            {
                "date": "2026-08-17",
                "weather_code": 1,
                "precipitation_probability_max": 10.0,
                "temperature_2m_max": 29.0,
                "temperature_2m_min": 21.0,
            }
        ],
    }
    assert requested_paths == ["/v1/search", "/v1/forecast"]
    client.close()
