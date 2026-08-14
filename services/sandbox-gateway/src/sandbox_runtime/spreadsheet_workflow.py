"""通用 CSV/XLSX 在一次性 Code Sandbox 中分析的受控工作流。"""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sandbox_runtime.artifact_store import ArtifactStore
from sandbox_runtime.models import ArtifactRecord, ExecutionReceipt, RequestContext, SandboxSession, utc_now
from sandbox_runtime.provider import SandboxProvider, provider_name


REMOTE_ANALYSIS = "/tmp/lazyx/output/analysis.json"
REMOTE_CHART = "/tmp/lazyx/output/chart.svg"
REMOTE_BRIEFING = "/tmp/lazyx/output/briefing.md"


def build_analysis_code(remote_input: str, source_name: str | None = None) -> str:
    """生成只读取指定输入、只写固定输出路径的分析程序。"""
    input_literal = json.dumps(remote_input)
    source_literal = json.dumps(source_name or Path(remote_input).name)
    return _ANALYSIS_CODE.replace("__INPUT_PATH__", input_literal).replace("__SOURCE_NAME__", source_literal)


_ANALYSIS_CODE = r'''
import csv
import html
import json
import math
import re
import statistics
from collections import Counter
from datetime import date, datetime
from pathlib import Path

input_path = Path(__INPUT_PATH__)
output_dir = Path("/tmp/lazyx/output")
output_dir.mkdir(parents=True, exist_ok=True)

def unique_headers(values):
    used = Counter()
    headers = []
    for index, value in enumerate(values, start=1):
        base = str(value).strip() if value is not None and str(value).strip() else f"column_{index}"
        used[base] += 1
        headers.append(base if used[base] == 1 else f"{base}_{used[base]}")
    return headers

def read_csv_file(path):
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.reader(source))
    if not rows:
        raise ValueError("Spreadsheet is empty")
    return [("CSV", unique_headers(rows[0]), rows[1:])]

def read_xlsx_file(path):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("XLSX analysis requires openpyxl in the sandbox image") from exc
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheets = []
    try:
        for worksheet in workbook.worksheets:
            rows = list(worksheet.iter_rows(values_only=True))
            if rows:
                sheets.append((worksheet.title, unique_headers(rows[0]), rows[1:]))
    finally:
        workbook.close()
    if not sheets:
        raise ValueError("Workbook contains no non-empty sheets")
    return sheets

def missing(value):
    return value is None or (isinstance(value, str) and not value.strip())

def parse_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text):
            try:
                return datetime.strptime(text.replace("/", "-"), "%Y-%m-%d").date().isoformat()
            except ValueError:
                return None
    return None

def parse_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text):
            try:
                return float(text)
            except ValueError:
                return None
    return None

def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)

def analyze_column(name, values):
    present = [value for value in values if not missing(value)]
    numeric = [parsed for value in present if (parsed := parse_number(value)) is not None]
    dates = [parsed for value in present if (parsed := parse_date(value))]
    booleans = [value for value in present if isinstance(value, bool)]
    result = {"name": name, "missing_count": len(values) - len(present), "non_missing_count": len(present)}
    if present and len(numeric) == len(present):
        q1, q3 = percentile(numeric, 0.25), percentile(numeric, 0.75)
        iqr = (q3 - q1) if q1 is not None and q3 is not None else 0
        low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        outliers = sum(value < low or value > high for value in numeric) if iqr else 0
        result.update({
            "inferred_type": "number",
            "min": min(numeric), "max": max(numeric),
            "mean": statistics.fmean(numeric), "median": statistics.median(numeric),
            "outlier_count": outliers,
        })
    elif present and len(dates) == len(present):
        result.update({"inferred_type": "date", "min_date": min(dates), "max_date": max(dates)})
    elif present and len(booleans) == len(present):
        counts = Counter(str(value).lower() for value in booleans)
        result.update({"inferred_type": "boolean", "top_values": counts.most_common(5)})
    else:
        counts = Counter(str(value).strip() for value in present)
        result.update({"inferred_type": "text" if present else "empty", "unique_count": len(counts), "top_values": counts.most_common(5)})
    return result

suffix = input_path.suffix.lower()
if suffix == ".csv":
    raw_sheets = read_csv_file(input_path)
elif suffix == ".xlsx":
    raw_sheets = read_xlsx_file(input_path)
else:
    raise ValueError("Only .csv and .xlsx files are supported")

sheets = []
total_rows = total_columns = missing_cells = anomaly_count = 0
for sheet_name, headers, rows in raw_sheets:
    normalized = [list(row) + [None] * max(0, len(headers) - len(row)) for row in rows]
    columns = []
    for index, header in enumerate(headers):
        column = analyze_column(header, [row[index] if index < len(row) else None for row in normalized])
        missing_cells += column["missing_count"]
        anomaly_count += column.get("outlier_count", 0)
        columns.append(column)
    total_rows += len(normalized)
    total_columns += len(headers)
    sheets.append({"name": sheet_name, "row_count": len(normalized), "column_count": len(headers), "columns": columns})

chart = {"title": "暂无可视化数据", "labels": [], "values": []}
for sheet in sheets:
    candidate = next((column for column in sheet["columns"] if column["inferred_type"] == "text" and column.get("top_values")), None)
    if candidate:
        chart = {
            "title": f"{sheet['name']} · {candidate['name']} 高频值",
            "labels": [item[0] for item in candidate["top_values"]],
            "values": [item[1] for item in candidate["top_values"]],
        }
        break
if not chart["labels"]:
    for sheet in sheets:
        numeric_columns = [column for column in sheet["columns"] if column["inferred_type"] == "number"]
        if numeric_columns:
            chart = {
                "title": f"{sheet['name']} · 数值列平均值",
                "labels": [column["name"] for column in numeric_columns[:5]],
                "values": [round(column["mean"], 2) for column in numeric_columns[:5]],
            }
            break

cell_count = max(total_rows * max(total_columns, 1), 1)
missing_ratio = round(missing_cells / cell_count * 100, 2)
notes = []
if missing_ratio > 10:
    notes.append(f"缺失值比例较高（{missing_ratio}%），建议核对采集与导入规则。")
if anomaly_count:
    notes.append(f"检测到 {anomaly_count} 个数值异常点，经营决策前应复核源记录。")
if not notes:
    notes.append("基础规则未发现明显的数据质量问题。")

analysis = {
    "schema_version": "1.0",
    "source_file": __SOURCE_NAME__,
    "summary": {
        "sheet_count": len(sheets), "row_count": total_rows, "column_count": total_columns,
        "missing_cells": missing_cells, "missing_ratio_percent": missing_ratio,
        "anomaly_count": anomaly_count, "primary_sheet": sheets[0]["name"],
    },
    "sheets": sheets,
    "chart": chart,
    "management_notes": notes,
}
(output_dir / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")

width, height, margin = 900, 480, 70
labels, values = chart["labels"], chart["values"]
maximum = max(values) if values else 1
gap = 18
bar_width = max(28, (width - margin * 2 - gap * max(len(values) - 1, 0)) / max(len(values), 1))
bars = []
for index, (label, value) in enumerate(zip(labels, values)):
    bar_height = (value / maximum) * 300 if maximum else 0
    x = margin + index * (bar_width + gap)
    y = 390 - bar_height
    bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="#2563eb" rx="5"/><text x="{x + bar_width/2:.1f}" y="{y - 8:.1f}" text-anchor="middle" font-size="14">{value:.2f}</text><text x="{x + bar_width/2:.1f}" y="420" text-anchor="middle" font-size="13">{html.escape(str(label)[:18])}</text>')
svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/><text x="450" y="35" text-anchor="middle" font-size="22" font-family="sans-serif">{html.escape(chart["title"])}</text><line x1="{margin}" y1="390" x2="{width-margin}" y2="390" stroke="#64748b"/>{"".join(bars)}</svg>'
(output_dir / "chart.svg").write_text(svg, encoding="utf-8")

summary = analysis["summary"]
lines = [
    "# Business Data Briefing", "", f"Source: `{analysis['source_file']}`", "",
    "## Overview", "", f"- Sheets: {summary['sheet_count']}", f"- Rows: {summary['row_count']}",
    f"- Columns: {summary['column_count']}", f"- Missing ratio: {summary['missing_ratio_percent']}%",
    f"- Numeric anomalies: {summary['anomaly_count']}", "", "## Management notes", "",
]
lines.extend(f"- {note}" for note in notes)
(output_dir / "briefing.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(json.dumps({"status": "ok", "analysis_path": str(output_dir / "analysis.json")}, ensure_ascii=False))
'''


@dataclass(frozen=True)
class SpreadsheetWorkflowResult:
    success: bool
    analysis: dict[str, Any] | None
    artifacts: tuple[ArtifactRecord, ...]
    receipt: ArtifactRecord
    error: str | None = None


class SpreadsheetAnalysisWorkflow:
    def __init__(self, provider: SandboxProvider, artifact_store: ArtifactStore) -> None:
        self.provider = provider
        self.artifact_store = artifact_store

    def run(
        self,
        input_path: Path | str,
        context: RequestContext,
        *,
        source_name: str | None = None,
    ) -> SpreadsheetWorkflowResult:
        source_path = Path(input_path)
        source_suffix = Path(source_name or source_path.name).suffix.lower()
        if source_suffix not in {".csv", ".xlsx"}:
            raise ValueError("Spreadsheet analysis only accepts .csv or .xlsx files.")
        input_bytes = source_path.read_bytes()
        remote_input = f"/tmp/lazyx/input/source{source_suffix}"
        input_record = self.artifact_store.put_bytes(
            context.attempt_id,
            f"source{source_suffix}",
            input_bytes,
            mimetypes.guess_type(source_path.name)[0] or "application/octet-stream",
        )
        receipt = ExecutionReceipt(
            receipt_id=f"receipt-{uuid4().hex}",
            context=context,
            provider=provider_name(self.provider),
            action="spreadsheet_analysis",
            started_at=utc_now(),
            inputs=[input_record],
        )
        session: SandboxSession | None = None
        output_records: list[ArtifactRecord] = []
        analysis: dict[str, Any] | None = None

        try:
            session = self.provider.create_code(context, ttl_seconds=300)
            receipt.sandbox_id = session.sandbox_id
            self.provider.put_file(session, remote_input, input_bytes)
            execution = self.provider.execute_code(
                session,
                build_analysis_code(remote_input, source_name or source_path.name),
                language="python",
                timeout_seconds=60,
            )
            if not execution.success:
                detail = execution.error_message or execution.stderr or "unknown error"
                raise RuntimeError(f"Spreadsheet analysis failed: {detail}")

            analysis_bytes = self.provider.get_file(session, REMOTE_ANALYSIS)
            loaded = json.loads(analysis_bytes)
            if not isinstance(loaded, dict):
                raise RuntimeError("Analysis artifact is not a JSON object.")
            analysis = loaded
            outputs = (
                ("analysis.json", analysis_bytes, "application/json"),
                ("chart.svg", self.provider.get_file(session, REMOTE_CHART), "image/svg+xml"),
                ("briefing.md", self.provider.get_file(session, REMOTE_BRIEFING), "text/markdown"),
            )
            output_records = [
                self.artifact_store.put_bytes(context.attempt_id, name, data, media_type)
                for name, data, media_type in outputs
            ]
            receipt.outputs.extend(output_records)
            receipt.status = "passed"
        except Exception as exc:
            receipt.status = "failed"
            receipt.error = f"{type(exc).__name__}: {exc}"
        finally:
            if session is not None:
                receipt.cleanup = self.provider.terminate(session)
                if not receipt.cleanup.destroyed:
                    receipt.status = "failed"
                    receipt.error = receipt.error or f"CleanupError: {receipt.cleanup.error or 'sandbox was not destroyed'}"
            receipt.finished_at = utc_now()

        receipt_record = self.artifact_store.put_json(
            context.attempt_id,
            "execution-receipt.json",
            receipt.to_dict(),
        )
        return SpreadsheetWorkflowResult(
            success=receipt.status == "passed",
            analysis=analysis,
            artifacts=tuple(output_records),
            receipt=receipt_record,
            error=receipt.error,
        )
