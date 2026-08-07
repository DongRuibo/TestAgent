"""销售分析业务闭环的 Artifact、Receipt 与清理测试。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from sandbox_runtime.artifact_store import ArtifactStore
from sandbox_runtime.models import (
    CleanupResult,
    ExecutionResult,
    RequestContext,
    SandboxKind,
    SandboxSession,
    utc_now,
)
from sandbox_runtime.sales_workflow import (
    REMOTE_CHART,
    REMOTE_INPUT,
    REMOTE_SUMMARY,
    SalesAnalysisWorkflow,
)


class FakeSalesProvider:
    def __init__(self, cleanup_ok: bool = True) -> None:
        self.files: dict[str, bytes] = {}
        self.cleanup_ok = cleanup_ok
        self.terminated = False

    def create_code(self, context: RequestContext, ttl_seconds: int = 300) -> SandboxSession:
        now = utc_now()
        return SandboxSession(
            sandbox_id="sbi-sales",
            provider="fake",
            kind=SandboxKind.CODE,
            template="fake",
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            metadata=context.metadata(),
            native=object(),
        )

    def put_file(self, session: SandboxSession, remote_path: str, data: bytes) -> None:
        self.files[remote_path] = bytes(data)

    def execute_code(
        self,
        session: SandboxSession,
        code: str,
        *,
        language: str = "python",
        timeout_seconds: float = 30,
    ) -> ExecutionResult:
        assert REMOTE_INPUT in self.files
        assert language == "python"
        summary = {
            "row_count": 2,
            "total_sales": 300.0,
            "top_region": "华东",
        }
        self.files[REMOTE_SUMMARY] = json.dumps(summary, ensure_ascii=False).encode()
        self.files[REMOTE_CHART] = b"<svg></svg>"
        return ExecutionResult(success=True, stdout='{"status":"ok"}\n')

    def get_file(self, session: SandboxSession, remote_path: str) -> bytes:
        return self.files[remote_path]

    def terminate(self, session: SandboxSession) -> CleanupResult:
        self.terminated = True
        return CleanupResult(session.sandbox_id, self.cleanup_ok)


def test_sales_workflow_persists_outputs_receipt_and_cleanup(tmp_path: Path) -> None:
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text(
        "month,region,product,amount\n2026-01,华东,A,100\n2026-02,华南,B,200\n",
        encoding="utf-8",
    )
    provider = FakeSalesProvider()
    store = ArtifactStore(tmp_path / "artifacts")
    context = RequestContext("tenant", "user", "task", "attempt-sales")

    result = SalesAnalysisWorkflow(provider, store).run(csv_path, context)

    assert result.success is True
    assert result.summary == {
        "row_count": 2,
        "total_sales": 300.0,
        "top_region": "华东",
    }
    assert provider.terminated is True
    assert {item.name for item in result.artifacts} == {"summary.json", "sales-chart.svg"}
    receipt = json.loads(store.read_bytes(result.receipt))
    assert receipt["status"] == "passed"
    assert receipt["sandbox_id"] == "sbi-sales"
    assert receipt["cleanup"]["destroyed"] is True
    assert len(receipt["inputs"]) == 1
    assert len(receipt["outputs"]) == 2


def test_sales_workflow_fails_when_cleanup_fails(tmp_path: Path) -> None:
    csv_path = tmp_path / "sales.csv"
    csv_path.write_text(
        "month,region,product,amount\n2026-01,华东,A,100\n",
        encoding="utf-8",
    )
    provider = FakeSalesProvider(cleanup_ok=False)
    store = ArtifactStore(tmp_path / "artifacts")
    context = RequestContext("tenant", "user", "task", "attempt-cleanup")

    result = SalesAnalysisWorkflow(provider, store).run(csv_path, context)

    assert result.success is False
    assert "CleanupError" in (result.error or "")
