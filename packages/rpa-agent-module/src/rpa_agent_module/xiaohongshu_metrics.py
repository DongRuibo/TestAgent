"""校验官方笔记列表导出，保留缺失值和统计口径。"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from zipfile import ZipFile

from openpyxl import load_workbook

from .models import XiaohongshuMetricsAccount, XiaohongshuMetricsRequest

HOME_URL = "https://creator.xiaohongshu.com/new/home?source=official"
METRICS_URL = "https://creator.xiaohongshu.com/statistics/data-analysis?source=official"
EXPORT_LIMIT = 1000
COUNTS = {
    "曝光": "impressions", "观看量": "views", "点赞": "likes", "评论": "comments",
    "收藏": "saves", "涨粉": "followers_gained", "分享": "shares", "弹幕": "danmaku",
}
HEADERS = {"笔记标题", "首次发布时间", "体裁", "封面点击率", "人均观看时长", *COUNTS}
UNAVAILABLE_FIELDS = ["content_id", "inquiries", "leads", "spend"]


def _number(value: Any, *, integer: bool = False, rate: bool = False) -> int | float | None:
    if value is None or str(value).strip() in {"", "-", "--", "—", "暂无数据"}:
        return None
    if isinstance(value, bool):
        raise ValueError("指标不能是布尔值")
    text = str(value).strip().replace(",", "")
    percent = text.endswith("%")
    number = float(text[:-1] if percent else text)
    if percent:
        if not rate:
            raise ValueError("计数指标不能是百分比")
        number /= 100
    if not math.isfinite(number) or number < 0 or (rate and number > 1):
        raise ValueError("指标数值超出范围")
    if integer and not number.is_integer():
        raise ValueError("计数指标必须为整数")
    return int(number) if integer else number


def parse_metrics_export(
    path: Path,
    *,
    query: XiaohongshuMetricsRequest,
    account: XiaohongshuMetricsAccount,
    source_total: int,
) -> dict[str, Any]:
    """只读取官方 XLSX 的值，拒绝错误表头、公式、越界日期和重复记录。"""

    if path.suffix.lower() != ".xlsx" or not 0 < path.stat().st_size <= 10 * 1024 * 1024:
        raise ValueError("导出文件格式或大小不符合预期")
    with ZipFile(path) as archive:
        if sum(entry.file_size for entry in archive.infolist()) > 30 * 1024 * 1024:
            raise ValueError("导出展开后体积超出范围")
    workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    try:
        sheet = workbook.worksheets[0]
        rows = sheet.iter_rows(values_only=True)
        header = None
        # 当前导出首行为“最多导出排序后前1000条笔记”，第二行为表头。
        for _ in range(5):
            candidate = next(rows, ())
            if HEADERS.issubset(set(candidate)):
                header = tuple(candidate)
                break
        if header is None or len(header) != len(set(header)):
            raise ValueError("未找到完整且唯一的官方笔记数据表头")
        for values in rows:
            if not any(value is not None for value in values):
                continue
            if len(records) >= EXPORT_LIMIT or len(values) != len(header):
                raise ValueError("导出记录数量或列数异常")
            if any(isinstance(value, str) and value.startswith("=") for value in values):
                raise ValueError("导出中出现公式，无法核实原始指标")
            row = dict(zip(header, values))
            title = str(row["笔记标题"] or "").strip()
            if not title:
                raise ValueError("笔记标题缺失")
            published = datetime.strptime(str(row["首次发布时间"]), "%Y年%m月%d日%H时%M分%S秒")
            if not query.start_date <= published.date() <= query.end_date:
                raise ValueError("导出包含筛选范围外的笔记，请核对筛选是否生效")
            published_at = published.replace(tzinfo=ZoneInfo("Asia/Shanghai")).isoformat()
            key = (title, published_at, str(row["体裁"]))
            if key in seen:
                raise ValueError("导出含无法区分的重复笔记，不能重复计数")
            seen.add(key)
            record = {
                "title": title, "published_at": published_at, "content_type": row["体裁"],
                **{field: _number(row[label], integer=True) for label, field in COUNTS.items()},
                "cover_click_rate": _number(row["封面点击率"], rate=True),
                "avg_view_seconds": _number(row["人均观看时长"]),
                **dict.fromkeys(UNAVAILABLE_FIELDS),
            }
            records.append(record)
    finally:
        workbook.close()
    if source_total < 0 or len(records) != min(source_total, EXPORT_LIMIT):
        raise ValueError("导出行数与后台总条数不一致，不能当成完整数据")
    selected = records[:query.max_posts]
    totals = {
        field: sum(row[field] for row in selected)
        if selected and all(row[field] is not None for row in selected) else None
        for field in COUNTS.values()
    }
    return {
        "schema_version": 1,
        "source": {"platform": "xiaohongshu", "kind": "creator_official_export", "url": METRICS_URL},
        "account": account.model_dump(),
        "account_verified_by": "creator_home_red_id",
        "collected_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "query": query.model_dump(mode="json"),
        "date_filter_kind": "first_published_at",
        "metrics_period": "unspecified_by_export",
        "traffic_scope": "unspecified_by_export",
        "source_total": source_total,
        "exported_count": len(records),
        "collected_count": len(selected),
        "truncated": len(selected) < source_total,
        "missing_fields": UNAVAILABLE_FIELDS,
        "notes": [
            "日期筛选表示笔记首发日期，不表示该日期区间新增流量。",
            "官方导出不区分自然和付费流量，不能据此计算投流 ROI。",
            "缺失值保留 null；汇总仅覆盖本次保留的笔记，不能当成全账号统计。",
            "官方导出最多 1000 篇，单次工具最多保留 50 篇；无笔记 ID，不能按标题自动关联投流。",
        ],
        "summary": {"totals": totals, "scope": "collected_posts_only"},
        "posts": selected,
    }
