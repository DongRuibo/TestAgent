"""装修业务工具：产生可追溯的结构化结果，不直接生成 UISpec。"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Callable, Protocol

import httpx

from tools.direct_spreadsheet import _resolve_spreadsheet


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


PREPARE_RENOVATION_PROJECT_TOOL = _tool(
    "prepare_renovation_project",
    "根据用户给出的城市、建筑面积和装修风格生成项目草稿，随后用 Generation UI 让用户填写并确认地址；不会直接创建项目。",
    {
        "city": {"type": "string", "description": "城市，例如杭州市。"},
        "area_m2": {"type": "number", "description": "建筑面积，单位平方米。"},
        "style": {"type": "string", "description": "装修风格。"},
    },
    ["city", "area_m2", "style"],
)

CALCULATE_MATERIAL_QUANTITIES_TOOL = _tool(
    "calculate_material_quantities",
    "根据房间长宽高和门窗面积确定性计算墙漆、瓷砖、地板采购量；输入不足时应先向用户追问。",
    {
        "rooms": {
            "type": "array",
            "description": (
                "房间数组；每项包含 name、length_m、width_m、height_m，"
                "可选 door_area_m2、window_area_m2、tile_floor、flooring。"
                "tile_floor/flooring 默认 true，表示分别估算两种地面方案。"
            ),
            "items": {"type": "object"},
        },
        "paint_coverage_m2_per_liter": {"type": "number", "description": "每升每遍覆盖面积，默认 10。"},
        "paint_coats": {"type": "integer", "description": "涂刷遍数，默认 2。"},
        "tile_waste_rate": {"type": "number", "description": "瓷砖损耗率，默认 0.10。"},
        "flooring_waste_rate": {"type": "number", "description": "地板损耗率，默认 0.08。"},
    },
    ["rooms"],
)

COMPARE_RENOVATION_QUOTES_TOOL = _tool(
    "compare_renovation_quotes",
    "比较 workspace 或 fixtures 中一份包含 2-4 家公司的装修报价 CSV，核算总价并标记缺项、算术错误和明显价格偏离。",
    {"path": {"type": "string", "description": "报价 CSV 相对路径。"}},
    ["path"],
)

PLAN_CONSTRUCTION_SCHEDULE_TOOL = _tool(
    "plan_construction_schedule",
    "从指定日期安排装修施工，并用结构化天气预报调整外墙和油漆工序；天气不可用时明确标记未核验。",
    {
        "city": {"type": "string", "description": "施工城市。"},
        "start_date": {"type": "string", "description": "开始日期，YYYY-MM-DD。"},
    },
    ["city", "start_date"],
)

REVIEW_RENOVATION_CONTRACT_TOOL = _tool(
    "review_renovation_contract",
    "检索当前对话附件中的装修合同原文，按固定规则标记付款、增项、工期、质保、违约和争议条款风险，并保留文档页码证据。",
    {},
    [],
)

RENOVATION_TOOL_DEFINITIONS = (
    PREPARE_RENOVATION_PROJECT_TOOL,
    CALCULATE_MATERIAL_QUANTITIES_TOOL,
    COMPARE_RENOVATION_QUOTES_TOOL,
    PLAN_CONSTRUCTION_SCHEDULE_TOOL,
    REVIEW_RENOVATION_CONTRACT_TOOL,
)


class PresentationRegistry(Protocol):
    def register(self, scenario: str, data: dict[str, Any]) -> dict[str, Any]: ...


SearchHandler = Callable[[str], dict[str, Any]]


class RenovationScenarioTools:
    def __init__(
        self,
        registry: PresentationRegistry,
        *,
        weather_client: WeatherForecastClient | None = None,
        contract_search: SearchHandler | None = None,
    ) -> None:
        self.registry = registry
        self.weather_client = weather_client or WeatherForecastClient()
        self.contract_search = contract_search

    def prepare_project(self, city: str, area_m2: Any, style: str) -> dict[str, Any]:
        try:
            data = {
                "city": _text(city, "city", 40),
                "area_m2": _positive_number(area_m2, "area_m2", maximum=10_000),
                "style": _text(style, "style", 60),
                "status": "待确认地址",
            }
        except ValueError as exc:
            return _error("INVALID_RENOVATION_PROJECT", str(exc))
        return _success(self.registry, "renovation_project_create", data)

    def calculate_materials(
        self,
        rooms: Any,
        paint_coverage_m2_per_liter: Any = 10,
        paint_coats: Any = 2,
        tile_waste_rate: Any = 0.10,
        flooring_waste_rate: Any = 0.08,
    ) -> dict[str, Any]:
        try:
            data = calculate_material_quantities(
                rooms,
                paint_coverage_m2_per_liter=paint_coverage_m2_per_liter,
                paint_coats=paint_coats,
                tile_waste_rate=tile_waste_rate,
                flooring_waste_rate=flooring_waste_rate,
            )
        except ValueError as exc:
            return _error("INVALID_ROOM_DIMENSIONS", str(exc))
        return _success(self.registry, "material_quantity", data)

    def compare_quotes(self, path: str) -> dict[str, Any]:
        try:
            data = compare_renovation_quotes(path)
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            return _error("INVALID_RENOVATION_QUOTES", str(exc))
        return _success(self.registry, "quote_comparison", data)

    def plan_schedule(self, city: str, start_date: str) -> dict[str, Any]:
        try:
            normalized_city = _text(city, "city", 40)
            normalized_start = _date(start_date, "start_date")
        except ValueError as exc:
            return _error("INVALID_CONSTRUCTION_SCHEDULE", str(exc))
        weather = self.weather_client.forecast(normalized_city, normalized_start, days=16)
        data = build_construction_schedule(normalized_city, normalized_start, weather)
        return _success(self.registry, "construction_schedule", data)

    def review_contract(self) -> dict[str, Any]:
        if self.contract_search is None:
            return _error(
                "CONTRACT_ATTACHMENT_REQUIRED",
                "请先在当前对话上传装修合同，等待附件状态变为已可用。",
                retryable=True,
            )
        result = self.contract_search(
            "装修合同 付款 验收 增项 变更 工期 延期 质保 保修 违约 解除 争议"
        )
        if result.get("success") is not True:
            return result
        candidates = result.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return _error(
                "CONTRACT_EVIDENCE_NOT_FOUND",
                "当前附件未检索到可用于审查的合同原文。",
                retryable=True,
            )
        data = review_contract_candidates(candidates)
        return _success(self.registry, "contract_review", data)

    def handlers(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return {
            "prepare_renovation_project": self.prepare_project,
            "calculate_material_quantities": self.calculate_materials,
            "compare_renovation_quotes": self.compare_quotes,
            "plan_construction_schedule": self.plan_schedule,
            "review_renovation_contract": self.review_contract,
        }


class WeatherForecastClient:
    """只访问固定 Open-Meteo 域名的结构化天气客户端。"""

    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, *, http_client: httpx.Client | None = None) -> None:
        self._client = http_client

    def forecast(self, city: str, start: date, *, days: int) -> dict[str, Any]:
        owns_client = self._client is None
        client = self._client or httpx.Client(
            timeout=httpx.Timeout(8.0, connect=3.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "TestAgent-Weather/0.1"},
        )
        try:
            location_response = client.get(
                self.GEOCODING_URL,
                params={"name": city, "count": 1, "language": "zh", "format": "json"},
            )
            location_response.raise_for_status()
            locations = location_response.json().get("results")
            if not isinstance(locations, list) or not locations:
                return _weather_error("未找到城市坐标。")
            location = locations[0]
            latitude = float(location["latitude"])
            longitude = float(location["longitude"])
            end = start + timedelta(days=days - 1)
            forecast_response = client.get(
                self.FORECAST_URL,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "daily": "weather_code,precipitation_probability_max,temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Shanghai",
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                },
            )
            forecast_response.raise_for_status()
            daily = forecast_response.json().get("daily")
            return _normalize_weather_daily(daily)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return _weather_error("结构化天气服务暂时不可用。")
        finally:
            if owns_client:
                client.close()


def calculate_material_quantities(
    rooms: Any,
    *,
    paint_coverage_m2_per_liter: Any,
    paint_coats: Any,
    tile_waste_rate: Any,
    flooring_waste_rate: Any,
) -> dict[str, Any]:
    if not isinstance(rooms, list) or not 1 <= len(rooms) <= 50:
        raise ValueError("rooms 必须包含 1-50 个房间。")
    coverage = _positive_number(paint_coverage_m2_per_liter, "paint_coverage_m2_per_liter", maximum=100)
    if isinstance(paint_coats, bool) or not isinstance(paint_coats, int) or not 1 <= paint_coats <= 5:
        raise ValueError("paint_coats 必须是 1-5 的整数。")
    tile_waste = _rate(tile_waste_rate, "tile_waste_rate")
    flooring_waste = _rate(flooring_waste_rate, "flooring_waste_rate")
    normalized_rooms = []
    totals = defaultdict(float)
    for index, room in enumerate(rooms):
        if not isinstance(room, dict):
            raise ValueError(f"rooms[{index}] 必须是对象。")
        allowed = {"name", "length_m", "width_m", "height_m", "door_area_m2", "window_area_m2", "tile_floor", "flooring"}
        unexpected = sorted(set(room) - allowed)
        if unexpected:
            raise ValueError(f"rooms[{index}] 包含未知字段：{', '.join(unexpected)}。")
        name = _text(room.get("name"), f"rooms[{index}].name", 40)
        length = _positive_number(room.get("length_m"), f"rooms[{index}].length_m", maximum=100)
        width = _positive_number(room.get("width_m"), f"rooms[{index}].width_m", maximum=100)
        height = _positive_number(room.get("height_m"), f"rooms[{index}].height_m", maximum=20)
        door = _nonnegative_number(room.get("door_area_m2", 0), f"rooms[{index}].door_area_m2", maximum=100)
        window = _nonnegative_number(room.get("window_area_m2", 0), f"rooms[{index}].window_area_m2", maximum=100)
        gross_wall = 2 * (length + width) * height
        if door + window >= gross_wall:
            raise ValueError(f"{name} 的门窗面积不能大于或等于墙面面积。")
        wall = gross_wall - door - window
        floor = length * width
        paint_liters = wall * paint_coats / coverage
        tile_area = floor * (1 + tile_waste) if room.get("tile_floor", True) is not False else 0
        flooring_area = floor * (1 + flooring_waste) if room.get("flooring", True) is not False else 0
        item = {
            "name": name,
            "wall_area_m2": _round(wall),
            "floor_area_m2": _round(floor),
            "paint_liters": _round(paint_liters),
            "tile_area_m2": _round(tile_area),
            "flooring_area_m2": _round(flooring_area),
        }
        normalized_rooms.append(item)
        for field in ("wall_area_m2", "floor_area_m2", "paint_liters", "tile_area_m2", "flooring_area_m2"):
            totals[field] += float(item[field])
    return {
        "metrics": {
            "paint_liters": _round(totals["paint_liters"]),
            "tile_area_m2": _round(totals["tile_area_m2"]),
            "flooring_area_m2": _round(totals["flooring_area_m2"]),
        },
        "rooms": normalized_rooms,
        "assumptions": {
            "paint_coverage_m2_per_liter": coverage,
            "paint_coats": paint_coats,
            "tile_waste_rate": tile_waste,
            "flooring_waste_rate": flooring_waste,
        },
    }


def compare_renovation_quotes(path: str) -> dict[str, Any]:
    source = _resolve_spreadsheet(path)
    if source.suffix.lower() != ".csv":
        raise ValueError("报价比较当前只支持 CSV。")
    if not source.exists() or not source.is_file():
        raise ValueError(f"报价文件不存在：{path}")
    required = {"company", "item", "unit", "quantity", "unit_price", "total_price"}
    rows: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        missing = sorted(required - set(fields))
        if missing:
            raise ValueError(f"报价 CSV 缺少列：{', '.join(missing)}。")
        for index, row in enumerate(reader, start=1):
            if index > 2_000:
                raise ValueError("报价 CSV 最多支持 2000 行。")
            rows.append(
                {
                    "company": _text(row.get("company"), f"第 {index} 行 company", 80),
                    "item": _text(row.get("item"), f"第 {index} 行 item", 100),
                    "unit": _text(row.get("unit"), f"第 {index} 行 unit", 20),
                    "quantity": _decimal(row.get("quantity"), f"第 {index} 行 quantity"),
                    "unit_price": _decimal(row.get("unit_price"), f"第 {index} 行 unit_price"),
                    "total_price": _decimal(row.get("total_price"), f"第 {index} 行 total_price"),
                }
            )
    companies = sorted({row["company"] for row in rows})
    if not 2 <= len(companies) <= 4:
        raise ValueError("报价比较需要 2-4 家公司。")
    items = sorted({row["item"] for row in rows})
    by_pair = {(row["company"], row["item"]): row for row in rows}
    anomalies: list[dict[str, str]] = []
    for row in rows:
        expected = row["quantity"] * row["unit_price"]
        tolerance = max(Decimal("1"), expected * Decimal("0.01"))
        if abs(row["total_price"] - expected) > tolerance:
            anomalies.append({"company": row["company"], "item": row["item"], "type": "算术不一致", "detail": f"数量×单价应为 {_money(expected)}，填报 {_money(row['total_price'])}"})
    for item in items:
        available = [by_pair[(company, item)]["unit_price"] for company in companies if (company, item) in by_pair]
        benchmark = Decimal(str(median([float(value) for value in available]))) if available else Decimal()
        for company in companies:
            row = by_pair.get((company, item))
            if row is None:
                anomalies.append({"company": company, "item": item, "type": "缺项", "detail": "该公司未提供此项目报价"})
            elif benchmark > 0 and abs(row["unit_price"] - benchmark) / benchmark > Decimal("0.30"):
                anomalies.append({"company": company, "item": item, "type": "单价偏离", "detail": "单价偏离同项目中位数超过 30%"})
    totals = {company: sum((row["total_price"] for row in rows if row["company"] == company), Decimal()) for company in companies}
    company_rows = [{"company": company, "total": _json_decimal(totals[company]), "item_count": sum(1 for row in rows if row["company"] == company)} for company in companies]
    comparison_rows = []
    comparison_fields = []
    keys: dict[str, str] = {}
    for index, item in enumerate(items):
        key = f"item_{index + 1}"
        keys[item] = key
        comparison_fields.append({"field": key, "label": item, "format": "currency"})
    for company in companies:
        comparison = {"company": company}
        for item in items:
            row = by_pair.get((company, item))
            comparison[keys[item]] = _json_decimal(row["total_price"]) if row else None
        comparison_rows.append(comparison)
    return {
        "source_file": source.name,
        "companies": company_rows,
        "items": comparison_rows,
        "comparison_fields": comparison_fields,
        "anomalies": anomalies[:100],
    }


def build_construction_schedule(city: str, start: date, weather: dict[str, Any]) -> dict[str, Any]:
    forecast = weather.get("days") if weather.get("success") is True else []
    weather_by_date = {item["date"]: item for item in forecast if isinstance(item, dict) and isinstance(item.get("date"), str)}
    definitions = [
        ("现场保护与拆改", 2, None),
        ("水电改造", 4, None),
        ("外墙施工", 3, "outdoor"),
        ("泥瓦工程", 5, None),
        ("油漆工程", 4, "paint"),
        ("地板安装", 2, None),
    ]
    cursor = start
    tasks = []
    warnings: list[str] = []
    for name, duration, weather_type in definitions:
        delayed = 0
        if weather_type and weather_by_date:
            while delayed < 14 and not _weather_window_suitable(cursor, duration, weather_type, weather_by_date):
                cursor += timedelta(days=1)
                delayed += 1
            if delayed:
                warnings.append(f"{name}因预报天气顺延 {delayed} 天。")
        end = cursor + timedelta(days=duration - 1)
        note = f"计划 {duration} 个自然日"
        if weather_type:
            note += "；已检查预报窗口" if weather_by_date else "；天气未核验，开工前需复查"
        tasks.append({"name": name, "start_date": cursor.isoformat(), "end_date": end.isoformat(), "date_range": f"{cursor.isoformat()} 至 {end.isoformat()}", "note": note})
        cursor = end + timedelta(days=1)
    if not weather_by_date:
        warnings.append("结构化天气服务不可用；外墙和油漆日期仅为初排，开工前必须复核。")
    return {
        "city": city,
        "start_date": start.isoformat(),
        "weather_status": "已核验" if weather_by_date else "未核验",
        "weather_provider": weather.get("provider") if weather_by_date else None,
        "tasks": tasks,
        "warnings": warnings,
    }


def review_contract_candidates(candidates: list[Any]) -> dict[str, Any]:
    rules = [
        ("付款与验收", "高", ("付款", "预付款"), "把付款节点与可验证的阶段验收结果绑定。"),
        ("增项与变更", "高", ("增项", "变更"), "约定书面变更、单价确认和总价上限。"),
        ("工期与延期", "中", ("工期", "延期"), "补充开竣工日期、顺延条件和每日违约责任。"),
        ("质保与维修", "中", ("质保", "保修"), "明确各工程质保期限、响应时间和责任主体。"),
        ("违约与解除", "高", ("违约", "解除"), "检查双方责任是否对等，并明确解除和退款机制。"),
        ("争议解决", "中", ("争议", "仲裁", "法院"), "确认争议管辖地点和解决方式是否可接受。"),
    ]
    risks = []
    valid_sources = []
    for candidate in candidates[:20]:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        document_name = candidate.get("document_name")
        if not isinstance(content, str) or not content.strip() or not isinstance(document_name, str):
            continue
        pages = candidate.get("pages")
        page_text = ",".join(str(page) for page in pages) if isinstance(pages, list) else "未知"
        valid_sources.append((content.strip(), f"{document_name} 第 {page_text} 页"))
    for title, severity, keywords, recommendation in rules:
        match = next(((content, source) for content, source in valid_sources if any(keyword in content for keyword in keywords)), None)
        if match is None:
            continue
        content, source = match
        risks.append({"severity": severity, "title": title, "evidence": _evidence_excerpt(content, keywords), "source": source, "recommendation": recommendation})
    return {
        "risks": risks,
        "source_count": len(valid_sources),
        "disclaimer": "这是基于已检索合同原文和固定规则的风险提示，不构成正式法律意见；未命中不代表没有风险。",
    }


def _success(registry: PresentationRegistry, scenario: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "presentation": registry.register(scenario, data)}


def _error(error_type: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {"success": False, "error_type": error_type, "message": message, "retryable": retryable}


def _weather_error(message: str) -> dict[str, Any]:
    return {"success": False, "message": message, "days": []}


def _normalize_weather_daily(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _weather_error("天气响应缺少 daily。")
    fields = [value.get(name) for name in ("time", "weather_code", "precipitation_probability_max", "temperature_2m_max", "temperature_2m_min")]
    if not all(isinstance(field, list) for field in fields):
        return _weather_error("天气响应字段无效。")
    length = len(fields[0])
    if length == 0 or any(len(field) != length for field in fields):
        return _weather_error("天气响应长度无效。")
    days = []
    for values in zip(*fields):
        day, code, rain, maximum, minimum = values
        if not isinstance(day, str):
            return _weather_error("天气日期无效。")
        days.append({"date": day, "weather_code": int(code), "precipitation_probability_max": float(rain), "temperature_2m_max": float(maximum), "temperature_2m_min": float(minimum)})
    return {"success": True, "provider": "Open-Meteo", "days": days}


def _weather_window_suitable(start: date, duration: int, kind: str, forecast: dict[str, dict[str, Any]]) -> bool:
    for offset in range(duration):
        item = forecast.get((start + timedelta(days=offset)).isoformat())
        if item is None:
            continue
        rain = float(item.get("precipitation_probability_max", 100))
        code = int(item.get("weather_code", 99))
        minimum = float(item.get("temperature_2m_min", -99))
        maximum = float(item.get("temperature_2m_max", 99))
        if rain >= 40 or code not in {0, 1, 2, 3}:
            return False
        if kind == "paint" and (minimum < 5 or maximum > 35):
            return False
    return True


def _evidence_excerpt(content: str, keywords: tuple[str, ...]) -> str:
    positions = [content.find(keyword) for keyword in keywords if keyword in content]
    start = max(0, min(positions) - 60) if positions else 0
    return " ".join(content[start : start + 220].split())


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空文本。")
    normalized = " ".join(value.split())
    if len(normalized) > maximum:
        raise ValueError(f"{field} 最多 {maximum} 个字符。")
    return normalized


def _positive_number(value: Any, field: str, *, maximum: float) -> int | float:
    number = _number(value, field)
    if number <= 0 or number > maximum:
        raise ValueError(f"{field} 必须大于 0 且不超过 {maximum}。")
    return int(number) if number.is_integer() else number


def _nonnegative_number(value: Any, field: str, *, maximum: float) -> int | float:
    number = _number(value, field)
    if number < 0 or number > maximum:
        raise ValueError(f"{field} 必须在 0 到 {maximum} 之间。")
    return int(number) if number.is_integer() else number


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字。")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} 必须是有限数字。")
    return number


def _rate(value: Any, field: str) -> float:
    number = _number(value, field)
    if not 0 <= number <= 0.5:
        raise ValueError(f"{field} 必须在 0 到 0.5 之间。")
    return number


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 字符串。")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field} 必须是有效的 YYYY-MM-DD 日期。") from exc


def _decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非负数字。")
    try:
        number = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{field} 必须是非负数字。") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field} 必须是非负有限数字。")
    return number


def _money(value: Decimal) -> str:
    return f"¥{value.quantize(Decimal('0.01'))}"


def _json_decimal(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


def _round(value: float) -> int | float:
    rounded = round(float(value), 2)
    return int(rounded) if rounded.is_integer() else rounded
