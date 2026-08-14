"""Gateway V0 的 HTTP、幂等、隔离和三类任务离线测试。"""

from __future__ import annotations

import json
import time
import base64
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from sandbox_gateway.api import create_app
from sandbox_gateway.auth import GatewayAuth, GatewayIdentity
from sandbox_gateway.models import ExecutionCreateRequest, ExecutionStatus
from sandbox_gateway.service import GatewayConfig, GatewayService
from sandbox_runtime.artifact_store import ArtifactStore
from sandbox_runtime.browser_workflow import BrowserWorkflowResult
from sandbox_runtime.models import (
    ArtifactRecord,
    CleanupResult,
    ExecutionResult,
    ExecutionReceipt,
    ProviderCapabilities,
    RequestContext,
    SandboxKind,
    SandboxSession,
    utc_now,
)


class FakeGatewayProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.remote_files: dict[str, bytes] = {}
        self.create_count = 0
        self.terminate_count = 0
        self.cleanup_expired_calls: list[int] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="fake",
            kinds=(SandboxKind.CODE, SandboxKind.BROWSER),
            languages=("python", "javascript"),
            network_modes=("PUBLIC", "SANDBOX", "VPC"),
            supports_file_transfer=True,
            supports_pause=False,
            supports_domain_egress_policy=True,
            max_ttl_seconds=300,
        )

    def create_code(self, context: RequestContext, ttl_seconds: int = 300) -> SandboxSession:
        self.create_count += 1
        now = utc_now()
        return SandboxSession(
            sandbox_id=f"fake-code-{self.create_count}",
            provider="fake",
            kind=SandboxKind.CODE,
            template="fake",
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            metadata=context.metadata(),
            native=object(),
        )

    def create_browser(self, context: RequestContext, ttl_seconds: int = 300) -> SandboxSession:
        self.create_count += 1
        now = utc_now()
        return SandboxSession(
            sandbox_id=f"fake-browser-{self.create_count}",
            provider="fake",
            kind=SandboxKind.BROWSER,
            template="fake",
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            metadata=context.metadata(),
            native=object(),
        )

    def put_file(self, session: SandboxSession, remote_path: str, data: bytes) -> None:
        self.remote_files[remote_path] = data

    def get_file(self, session: SandboxSession, remote_path: str) -> bytes:
        return self.remote_files.get(remote_path, b"gateway-artifact")

    def execute_code(
        self,
        session: SandboxSession,
        code: str,
        *,
        language: str = "python",
        timeout_seconds: float = 30,
    ) -> ExecutionResult:
        if "/tmp/lazyx/input/sales.csv" in self.remote_files:
            self.remote_files["/tmp/lazyx/output/summary.json"] = json.dumps(
                {"row_count": 1, "total_sales": 100.0},
            ).encode()
            self.remote_files["/tmp/lazyx/output/sales-chart.svg"] = b"<svg />"
        if any(path.startswith("/tmp/lazyx/input/source.") for path in self.remote_files):
            self.remote_files["/tmp/lazyx/output/analysis.json"] = json.dumps({
                "source_file": "source.csv",
                "summary": {"sheet_count": 1, "row_count": 2, "column_count": 2},
                "sheets": [],
                "chart": {"labels": [], "values": []},
                "management_notes": [],
            }).encode()
            self.remote_files["/tmp/lazyx/output/chart.svg"] = b"<svg />"
            self.remote_files["/tmp/lazyx/output/briefing.md"] = b"# Briefing\n"
        self.remote_files["/tmp/output.txt"] = b"gateway-artifact"
        return ExecutionResult(success=True, stdout="hello from sandbox\n")

    def browser_urls(self, session: SandboxSession):
        return "https://fake-browser", "", {}

    def terminate(self, session: SandboxSession) -> CleanupResult:
        self.terminate_count += 1
        return CleanupResult(session.sandbox_id, True)

    def cleanup_expired(self, max_age_seconds: int):
        self.cleanup_expired_calls.append(max_age_seconds)
        return [CleanupResult("fake-orphan", True)]


class FakeBrowserWorkflow:
    def __init__(self, provider, store: ArtifactStore) -> None:
        self.store = store

    def run(self, url: str, context: RequestContext, *, max_text_chars: int = 12_000):
        content = self.store.put_bytes(
            context.attempt_id,
            "page-content.txt",
            b"fake browser content",
            "text/plain",
        )
        receipt = ExecutionReceipt(
            receipt_id="receipt-fake-browser",
            context=context,
            provider="fake",
            action="browser_public_extract",
            started_at=utc_now(),
            sandbox_id="fake-browser-1",
            status="passed",
            outputs=[content],
            cleanup=CleanupResult("fake-browser-1", True),
            finished_at=utc_now(),
        )
        receipt_record = self.store.put_json(context.attempt_id, "execution-receipt.json", receipt.to_dict())
        return BrowserWorkflowResult(
            success=True,
            source_url=url,
            final_url=url,
            status_code=200,
            title="Fake page",
            text_preview="fake browser content",
            text_total_chars=20,
            page_height=1_200,
            screenshot_height=1_200,
            screenshot_full_page=True,
            screenshot_truncated=False,
            screenshot_stitched=False,
            screenshot_scroll_steps=1,
            trust="untrusted",
            instructions_executable=False,
            artifacts=(content,),
            receipt=receipt_record,
            cleanup=CleanupResult("fake-browser-1", True),
        )


def _build(tmp_path: Path):
    provider = FakeGatewayProvider()
    service = GatewayService(
        {"fake": provider},
        database_path=tmp_path / "gateway.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config=GatewayConfig(max_workers=1),
        browser_workflow_factory=lambda p, s: FakeBrowserWorkflow(p, s),
    )
    app = create_app(
        service,
        GatewayAuth(
            {
                "agent-token": GatewayIdentity("tenant-a", "agent-a"),
                "other-token": GatewayIdentity("tenant-b", "agent-b"),
            }
        ),
    )
    return service, provider, TestClient(app)


def _wait(client: TestClient, execution_id: str, token: str = "agent-token") -> dict:
    for _ in range(100):
        response = client.get(
            f"/v1/executions/{execution_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("Gateway execution did not finish")


def test_gateway_code_upload_idempotency_artifact_and_cleanup(tmp_path: Path) -> None:
    service, provider, client = _build(tmp_path)
    headers = {"Authorization": "Bearer agent-token"}
    uploaded = client.post(
        "/v1/files",
        headers=headers,
        json={
            "name": "input.txt",
            "media_type": "text/plain",
            "content_base64": "aGVsbG8=",
        },
    )
    assert uploaded.status_code == 201
    file_id = uploaded.json()["file_id"]
    request = {
        "task_type": "code",
        "source": "print('hello')",
        "input_files": [{"file_id": file_id, "remote_path": "/tmp/input.txt"}],
        "output_paths": ["/tmp/output.txt"],
        "provider_preference": "fake",
    }
    first = client.post(
        "/v1/executions",
        headers={**headers, "Idempotency-Key": "same-request"},
        json=request,
    )
    second = client.post(
        "/v1/executions",
        headers={**headers, "Idempotency-Key": "same-request"},
        json=request,
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["execution_id"] == second.json()["execution_id"]

    result = _wait(client, first.json()["execution_id"])
    assert result["status"] == "succeeded"
    assert result["result"]["stdout"] == "hello from sandbox\n"
    assert result["receipt"]["cleanup"]["destroyed"] is True
    assert provider.create_count == 1
    assert provider.terminate_count == 1

    artifact = client.get(
        f"/v1/executions/{first.json()['execution_id']}/artifacts/output-0-output.txt",
        headers=headers,
    )
    assert artifact.status_code == 200
    assert artifact.content == b"gateway-artifact"
    service.shutdown()


def test_gateway_rejects_idempotency_conflict_and_cross_tenant_read(tmp_path: Path) -> None:
    service, _provider, client = _build(tmp_path)
    request = {
        "task_type": "code",
        "source": "print('one')",
        "provider_preference": "fake",
    }
    headers = {"Authorization": "Bearer agent-token", "Idempotency-Key": "conflict"}
    first = client.post("/v1/executions", headers=headers, json=request)
    assert first.status_code == 202
    conflict = client.post(
        "/v1/executions",
        headers=headers,
        json={**request, "source": "print('two')"},
    )
    assert conflict.status_code == 409

    hidden = client.get(
        f"/v1/executions/{first.json()['execution_id']}",
        headers={"Authorization": "Bearer other-token"},
    )
    assert hidden.status_code == 404
    service.shutdown()


def test_gateway_runs_sales_and_browser_task_types(tmp_path: Path) -> None:
    service, provider, client = _build(tmp_path)
    headers = {"Authorization": "Bearer agent-token"}
    uploaded = client.post(
        "/v1/files",
        headers=headers,
        json={
            "name": "sales.csv",
            "media_type": "text/csv",
            "content_base64": base64.b64encode(
                b"month,region,product,amount\n2026-01,East,A,100\n"
            ).decode(),
        },
    )
    assert uploaded.status_code == 201
    sales = client.post(
        "/v1/executions",
        headers=headers,
        json={
            "task_type": "sales_analysis",
            "input_files": [{"file_id": uploaded.json()["file_id"], "remote_path": "/tmp/input.csv"}],
            "provider_preference": "fake",
        },
    )
    assert sales.status_code == 202
    assert _wait(client, sales.json()["execution_id"])["status"] == "succeeded"

    spreadsheet = client.post(
        "/v1/executions",
        headers=headers,
        json={
            "task_type": "spreadsheet_analysis",
            "input_files": [{"file_id": uploaded.json()["file_id"], "remote_path": "/tmp/input.csv"}],
            "provider_preference": "fake",
        },
    )
    assert spreadsheet.status_code == 202
    spreadsheet_result = _wait(client, spreadsheet.json()["execution_id"])
    assert spreadsheet_result["status"] == "succeeded"
    assert spreadsheet_result["result"]["analysis"]["summary"]["row_count"] == 2
    assert {item["name"] for item in spreadsheet_result["artifacts"]} == {
        "analysis.json", "chart.svg", "briefing.md", "execution-receipt.json"
    }

    browser = client.post(
        "/v1/executions",
        headers=headers,
        json={
            "task_type": "browser_public",
            "url": "https://example.com",
            "network_profile": "public_readonly",
            "provider_preference": "fake",
        },
    )
    assert browser.status_code == 202
    browser_result = _wait(client, browser.json()["execution_id"])
    assert browser_result["status"] == "succeeded"
    assert browser_result["result"]["title"] == "Fake page"
    assert provider.terminate_count >= 1
    service.shutdown()


def test_gateway_business_briefing_adds_pdf_and_pptx(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sandbox_gateway.service.render_pdf", lambda analysis: b"%PDF-fake")
    monkeypatch.setattr("sandbox_gateway.service.render_pptx", lambda analysis: b"PK-pptx-fake")
    service, _provider, client = _build(tmp_path)
    headers = {"Authorization": "Bearer agent-token"}
    uploaded = client.post(
        "/v1/files",
        headers=headers,
        json={
            "name": "operations.csv",
            "media_type": "text/csv",
            "content_base64": base64.b64encode(b"department,amount\nDesign,100\n").decode(),
        },
    )
    response = client.post(
        "/v1/executions",
        headers=headers,
        json={
            "task_type": "spreadsheet_analysis",
            "input_files": [{"file_id": uploaded.json()["file_id"], "remote_path": "/tmp/input.csv"}],
            "generate_briefing": True,
            "provider_preference": "fake",
        },
    )

    result = _wait(client, response.json()["execution_id"])

    assert result["status"] == "succeeded"
    assert {item["name"] for item in result["artifacts"]} == {
        "analysis.json", "chart.svg", "briefing.md", "briefing.pdf",
        "briefing.pptx", "execution-receipt.json",
    }
    assert {item["name"] for item in result["receipt"]["outputs"]} >= {
        "briefing.pdf", "briefing.pptx"
    }
    service.shutdown()


def test_gateway_recovery_marks_inflight_and_scans_managed_orphans(tmp_path: Path) -> None:
    service, provider, _client = _build(tmp_path)
    request = ExecutionCreateRequest(
        task_type="code",
        source="print('never started')",
        provider_preference="fake",
    )
    record = service.repository.create_execution(
        "exec-restart",
        "tenant-a",
        "agent-a",
        request,
        utc_now(),
        "restart-key",
    )
    record.provider = "fake"
    record.status = ExecutionStatus.RUNNING
    record.sandbox_id = "fake-stale"
    service.repository.save_execution(record)

    recovered = service.recover_after_restart()
    assert recovered == ["exec-restart"]
    recovered_record = service.repository.get_execution("exec-restart")
    assert recovered_record is not None
    assert recovered_record.status is ExecutionStatus.FAILED
    assert recovered_record.error_type == "GATEWAY_RESTARTED"

    cleanup = service.cleanup_orphans(max_age_seconds=600)
    assert cleanup["fake"][0]["sandbox_id"] == "fake-orphan"
    assert provider.cleanup_expired_calls == [600]
    service.shutdown()
