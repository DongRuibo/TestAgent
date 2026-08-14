"""Agent 直连 AGSX 表格工具的路径、产物和清理测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from sandbox_runtime.artifact_store import ArtifactStore
from tools.direct_spreadsheet import analyze_spreadsheet, generate_business_briefing


def test_direct_spreadsheet_rejects_path_escape() -> None:
    result = analyze_spreadsheet("../.env")

    assert result["success"] is False
    assert result["error_type"] == "INVALID_SPREADSHEET_PATH"


def test_direct_spreadsheet_returns_analysis_artifacts_and_cleanup(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    class FakeWorkflow:
        def run(self, source, context, *, source_name=None):
            analysis = {"source_file": source_name, "summary": {"row_count": 3}}
            output = store.put_json(context.attempt_id, "analysis.json", analysis)
            receipt = store.put_json(
                context.attempt_id,
                "execution-receipt.json",
                {
                    "provider": "tencent-agsx",
                    "sandbox_id": "sandbox-sheet",
                    "status": "passed",
                    "cleanup": {"destroyed": True, "error": None},
                    "outputs": [output.to_dict()],
                },
            )
            return SimpleNamespace(
                success=True,
                analysis=analysis,
                artifacts=(output,),
                receipt=receipt,
                error=None,
            )

    result = analyze_spreadsheet(
        "fixtures/sales.csv",
        workflow=FakeWorkflow(),
        artifact_store=store,
    )

    assert result["success"] is True
    assert result["analysis"]["summary"]["row_count"] == 3
    assert result["sandbox_destroyed"] is True
    assert {item["name"] for item in result["artifacts"]} == {
        "analysis.json",
        "execution-receipt.json",
    }


def test_direct_business_briefing_adds_pdf_and_pptx(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    class FakeWorkflow:
        def run(self, source, context, *, source_name=None):
            analysis = {"source_file": source_name, "summary": {}}
            base = store.put_json(context.attempt_id, "analysis.json", analysis)
            receipt = store.put_json(
                context.attempt_id,
                "execution-receipt.json",
                {
                    "provider": "tencent-agsx",
                    "sandbox_id": "sandbox-sheet",
                    "status": "passed",
                    "cleanup": {"destroyed": True, "error": None},
                    "outputs": [base.to_dict()],
                },
            )
            return SimpleNamespace(
                success=True,
                analysis=analysis,
                artifacts=(base,),
                receipt=receipt,
                error=None,
            )

    result = generate_business_briefing(
        "fixtures/sales.csv",
        workflow=FakeWorkflow(),
        artifact_store=store,
        pdf_renderer=lambda _analysis: b"pdf",
        pptx_renderer=lambda _analysis: b"pptx",
    )

    assert result["success"] is True
    assert {item["name"] for item in result["artifacts"]} == {
        "analysis.json",
        "briefing.pdf",
        "briefing.pptx",
        "execution-receipt.json",
    }
    receipt = json.loads((tmp_path / result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["status"] == "passed"
