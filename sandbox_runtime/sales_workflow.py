"""销售 CSV 在 AGSX 中分析并生成 Artifact/Receipt 的端到端工作流。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sandbox_runtime.artifact_store import ArtifactStore
from sandbox_runtime.models import (
    ArtifactRecord,
    ExecutionReceipt,
    RequestContext,
    SandboxSession,
    utc_now,
)
from sandbox_runtime.provider import SandboxProvider, provider_name


REMOTE_INPUT = "/tmp/lazyx/input/sales.csv"
REMOTE_SUMMARY = "/tmp/lazyx/output/summary.json"
REMOTE_CHART = "/tmp/lazyx/output/sales-chart.svg"

SALES_ANALYSIS_CODE = r'''
import csv
import html
import json
from collections import defaultdict
from pathlib import Path

input_path = Path("/tmp/lazyx/input/sales.csv")
output_dir = Path("/tmp/lazyx/output")
output_dir.mkdir(parents=True, exist_ok=True)

rows = []
with input_path.open("r", encoding="utf-8-sig", newline="") as source:
    reader = csv.DictReader(source)
    required = {"month", "region", "product", "amount"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError(f"CSV columns must include {sorted(required)}")
    for line_number, row in enumerate(reader, start=2):
        try:
            amount = float(row["amount"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid amount at line {line_number}") from exc
        rows.append({**row, "amount": amount})

if not rows:
    raise ValueError("Sales CSV is empty")

monthly = defaultdict(float)
regional = defaultdict(float)
product = defaultdict(float)
for row in rows:
    monthly[row["month"]] += row["amount"]
    regional[row["region"]] += row["amount"]
    product[row["product"]] += row["amount"]

months = sorted(monthly)
first_value = monthly[months[0]]
last_value = monthly[months[-1]]
growth = None if first_value == 0 else round((last_value / first_value - 1) * 100, 2)
total = round(sum(item["amount"] for item in rows), 2)
summary = {
    "row_count": len(rows),
    "total_sales": total,
    "average_order": round(total / len(rows), 2),
    "first_month": months[0],
    "last_month": months[-1],
    "growth_percent": growth,
    "top_region": max(regional, key=regional.get),
    "top_product": max(product, key=product.get),
    "monthly_sales": {month: round(monthly[month], 2) for month in months},
    "regional_sales": {name: round(value, 2) for name, value in sorted(regional.items())},
}

summary_path = output_dir / "summary.json"
summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

width, height = 760, 420
margin = 60
plot_width = width - margin * 2
plot_height = height - margin * 2
maximum = max(monthly.values()) or 1
bar_gap = 18
bar_width = max(20, (plot_width - bar_gap * (len(months) - 1)) / len(months))
bars = []
for index, month in enumerate(months):
    value = monthly[month]
    bar_height = value / maximum * (plot_height - 35)
    x = margin + index * (bar_width + bar_gap)
    y = height - margin - bar_height
    bars.append(
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="#2563eb" rx="4"/>'
        f'<text x="{x + bar_width / 2:.1f}" y="{y - 8:.1f}" text-anchor="middle" font-size="12">{value:.0f}</text>'
        f'<text x="{x + bar_width / 2:.1f}" y="{height - margin + 22:.1f}" text-anchor="middle" font-size="12">{html.escape(month)}</text>'
    )

svg = (
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
    '<rect width="100%" height="100%" fill="white"/>'
    '<text x="380" y="30" text-anchor="middle" font-size="20" font-family="sans-serif">Monthly Sales</text>'
    f'<line x1="{margin}" y1="{height - margin}" x2="{width - margin}" y2="{height - margin}" stroke="#475569"/>'
    + "".join(bars)
    + '</svg>'
)
(output_dir / "sales-chart.svg").write_text(svg, encoding="utf-8")
print(json.dumps({"status": "ok", "summary_path": str(summary_path)}, ensure_ascii=False))
'''


@dataclass(frozen=True)
class SalesWorkflowResult:
    success: bool
    summary: dict[str, Any] | None
    artifacts: tuple[ArtifactRecord, ...]
    receipt: ArtifactRecord
    error: str | None = None


class SalesAnalysisWorkflow:
    def __init__(self, provider: SandboxProvider, artifact_store: ArtifactStore) -> None:
        self.provider = provider
        self.artifact_store = artifact_store

    def run(self, csv_path: Path | str, context: RequestContext) -> SalesWorkflowResult:
        input_bytes = Path(csv_path).read_bytes()
        input_record = self.artifact_store.put_bytes(
            context.attempt_id,
            "sales-input.csv",
            input_bytes,
            "text/csv",
        )
        receipt = ExecutionReceipt(
            receipt_id=f"receipt-{uuid4().hex}",
            context=context,
            provider=provider_name(self.provider),
            action="sales_csv_analysis",
            started_at=utc_now(),
            inputs=[input_record],
        )
        session: SandboxSession | None = None
        output_records: list[ArtifactRecord] = []
        summary: dict[str, Any] | None = None

        try:
            session = self.provider.create_code(context, ttl_seconds=300)
            receipt.sandbox_id = session.sandbox_id
            self.provider.put_file(session, REMOTE_INPUT, input_bytes)
            execution = self.provider.execute_code(
                session,
                SALES_ANALYSIS_CODE,
                language="python",
                timeout_seconds=30,
            )
            if not execution.success:
                detail = execution.error_message or execution.stderr or "unknown error"
                raise RuntimeError(f"Sales analysis failed: {detail}")

            summary_bytes = self.provider.get_file(session, REMOTE_SUMMARY)
            chart_bytes = self.provider.get_file(session, REMOTE_CHART)
            loaded = json.loads(summary_bytes)
            if not isinstance(loaded, dict):
                raise RuntimeError("Summary artifact is not a JSON object.")
            summary = loaded
            output_records = [
                self.artifact_store.put_bytes(
                    context.attempt_id,
                    "summary.json",
                    summary_bytes,
                    "application/json",
                ),
                self.artifact_store.put_bytes(
                    context.attempt_id,
                    "sales-chart.svg",
                    chart_bytes,
                    "image/svg+xml",
                ),
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
                    cleanup_error = receipt.cleanup.error or "sandbox was not destroyed"
                    receipt.error = receipt.error or f"CleanupError: {cleanup_error}"
            receipt.finished_at = utc_now()

        receipt_record = self.artifact_store.put_json(
            context.attempt_id,
            "execution-receipt.json",
            receipt.to_dict(),
        )
        return SalesWorkflowResult(
            success=receipt.status == "passed",
            summary=summary,
            artifacts=tuple(output_records),
            receipt=receipt_record,
            error=receipt.error,
        )
