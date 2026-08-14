"""把沙箱返回的可信结构化结果渲染为固定版式 PDF/PPTX。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent


def render_pdf(analysis: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font_path = _briefing_font_path()
    pdfmetrics.registerFont(TTFont("BriefingFont", str(font_path)))
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "BriefingTitle", parent=styles["Title"], fontName="BriefingFont",
        fontSize=24, leading=30, textColor=colors.HexColor("#0f172a"), alignment=TA_CENTER,
    )
    heading = ParagraphStyle(
        "BriefingHeading", parent=styles["Heading2"], fontName="BriefingFont",
        fontSize=15, leading=20, textColor=colors.HexColor("#1d4ed8"), spaceBefore=8,
    )
    body = ParagraphStyle(
        "BriefingBody", parent=styles["BodyText"], fontName="BriefingFont",
        fontSize=10.5, leading=16, textColor=colors.HexColor("#334155"),
    )
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    )
    summary = analysis.get("summary") or {}
    story = [
        Paragraph("经营数据简报", title),
        Paragraph(f"数据源：{_escape(analysis.get('source_file', '-'))}", body),
        Spacer(1, 8 * mm),
        Paragraph("核心概览", heading),
    ]
    metrics = [
        ["工作表", "行数", "列数", "缺失率", "异常值"],
        [
            summary.get("sheet_count", 0), summary.get("row_count", 0),
            summary.get("column_count", 0), f"{summary.get('missing_ratio_percent', 0)}%",
            summary.get("anomaly_count", 0),
        ],
    ]
    table = Table(metrics, colWidths=[32 * mm] * 5)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbeafe")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
        ("FONTNAME", (0, 0), (-1, -1), "BriefingFont"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.extend([table, Spacer(1, 6 * mm), Paragraph("数据质量与经营提示", heading)])
    for note in analysis.get("management_notes") or ["暂无管理提示。"]:
        story.append(Paragraph(f"• {_escape(note)}", body))
    for sheet in (analysis.get("sheets") or [])[:3]:
        story.extend([
            Spacer(1, 3 * mm),
            Paragraph(f"工作表：{_escape(sheet.get('name', '-'))}", heading),
            Paragraph(
                f"{sheet.get('row_count', 0)} 行，{sheet.get('column_count', 0)} 列。"
                + "；".join(
                    f"{_escape(column.get('name', '-'))}: {column.get('inferred_type', '-')}，缺失 {column.get('missing_count', 0)}"
                    for column in (sheet.get("columns") or [])[:8]
                ),
                body,
            ),
        ])
    document.build(story)
    return buffer.getvalue()


def render_pptx(analysis: dict[str, Any], *, qa_dir: Path | None = None) -> bytes:
    artifact_tool_root = os.getenv("ARTIFACT_TOOL_ROOT", "").strip()
    if not artifact_tool_root:
        raise RuntimeError("ARTIFACT_TOOL_ROOT is required to render briefing.pptx")
    node = os.getenv("ARTIFACT_TOOL_NODE", "node").strip() or "node"
    script = PACKAGE_ROOT / "render_briefing.mjs"
    with tempfile.TemporaryDirectory(prefix="lazyx-briefing-") as temp_name:
        temp = Path(temp_name)
        input_path = temp / "analysis.json"
        output_path = temp / "briefing.pptx"
        input_path.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
        resolved_qa = qa_dir or temp / "qa"
        completed = subprocess.run(
            [node, str(script), artifact_tool_root, str(input_path), str(output_path), str(resolved_qa)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-2000:]
            raise RuntimeError(f"PPTX renderer failed: {detail}")
        return output_path.read_bytes()


def _escape(value: Any) -> str:
    import html

    return html.escape(str(value))


def _briefing_font_path() -> Path:
    configured = os.getenv("BRIEFING_FONT_PATH", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    raise RuntimeError("Set BRIEFING_FONT_PATH to a Unicode TTF font used by PDF rendering")
