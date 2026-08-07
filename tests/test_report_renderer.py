"""经营简报受控 PDF 渲染测试。"""

from sandbox_runtime.report_renderer import render_pdf


def test_render_pdf_returns_a_pdf_with_business_metrics() -> None:
    content = render_pdf({
        "source_file": "operations.csv",
        "summary": {
            "sheet_count": 1, "row_count": 12, "column_count": 4,
            "missing_ratio_percent": 1.5, "anomaly_count": 2,
        },
        "sheets": [{
            "name": "CSV", "row_count": 12, "column_count": 4,
            "columns": [{"name": "amount", "inferred_type": "number", "missing_count": 0}],
        }],
        "management_notes": ["Review two unusual amounts."],
    })

    assert content.startswith(b"%PDF-")
    assert len(content) > 5_000
