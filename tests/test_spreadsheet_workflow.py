"""通用 CSV/XLSX 沙箱工作流的文件、产物和清理测试。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from sandbox_runtime.artifact_store import ArtifactStore
from sandbox_runtime.models import CleanupResult, ExecutionResult, RequestContext, SandboxKind, SandboxSession, utc_now
from sandbox_runtime.spreadsheet_workflow import SpreadsheetAnalysisWorkflow, build_analysis_code


class FakeSpreadsheetProvider:
    provider_name = "fake-spreadsheet"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.terminated = False

    def create_code(self, context, ttl_seconds=300):
        now = utc_now()
        return SandboxSession("sandbox-sheet", self.provider_name, SandboxKind.CODE, "fake", now, now + timedelta(seconds=ttl_seconds), context.metadata(), object())

    def put_file(self, session, remote_path, data):
        self.files[remote_path] = data

    def execute_code(self, session, code, *, language="python", timeout_seconds=30):
        source = next(path for path in self.files if path.startswith("/tmp/lazyx/input/source."))
        self.files["/tmp/lazyx/output/analysis.json"] = json.dumps({
            "source_file": Path(source).name,
            "summary": {"sheet_count": 1, "row_count": 3, "column_count": 2},
            "sheets": [], "chart": {"labels": [], "values": []}, "management_notes": [],
        }).encode()
        self.files["/tmp/lazyx/output/chart.svg"] = b"<svg />"
        self.files["/tmp/lazyx/output/briefing.md"] = b"# Briefing\n"
        return ExecutionResult(success=True, stdout="ok\n")

    def get_file(self, session, remote_path):
        return self.files[remote_path]

    def terminate(self, session):
        self.terminated = True
        return CleanupResult(session.sandbox_id, True)


@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
def test_spreadsheet_workflow_accepts_csv_and_xlsx_and_cleans_up(tmp_path: Path, suffix: str) -> None:
    source = tmp_path / f"input{suffix}"
    source.write_bytes(b"name,value\nA,1\n" if suffix == ".csv" else b"PK-fake-xlsx")
    provider = FakeSpreadsheetProvider()
    store = ArtifactStore(tmp_path / "artifacts")
    result = SpreadsheetAnalysisWorkflow(provider, store).run(
        source,
        RequestContext("tenant", "agent", "task", f"attempt-{suffix[1:]}"),
    )

    assert result.success is True
    assert result.analysis["summary"]["row_count"] == 3
    assert {item.name for item in result.artifacts} == {"analysis.json", "chart.svg", "briefing.md"}
    assert provider.terminated is True
    receipt = json.loads(store.read_bytes(result.receipt))
    assert receipt["cleanup"]["destroyed"] is True


def test_generated_analysis_code_infers_csv_numbers_and_dates(tmp_path: Path) -> None:
    source = tmp_path / "operations.csv"
    source.write_text(
        "date,department,amount\n2026-08-01,Design,1200.5\n2026-08-02,Sales,3000\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    code = build_analysis_code(str(source), source.name).replace("/tmp/lazyx/output", str(output))

    exec(compile(code, "spreadsheet-analysis", "exec"), {})

    analysis = json.loads((output / "analysis.json").read_text(encoding="utf-8"))
    columns = {item["name"]: item for item in analysis["sheets"][0]["columns"]}
    assert analysis["source_file"] == "operations.csv"
    assert columns["date"]["inferred_type"] == "date"
    assert columns["amount"]["inferred_type"] == "number"
    assert columns["amount"]["mean"] == 2100.25
