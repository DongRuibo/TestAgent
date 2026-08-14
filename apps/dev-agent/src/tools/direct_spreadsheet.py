"""Agent 进程内直连 AGSX 的通用 CSV/XLSX 分析工具。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from dotenv import load_dotenv

from sandbox_runtime import ArtifactStore, RequestContext
from sandbox_runtime.report_renderer import render_pdf, render_pptx
from sandbox_runtime.spreadsheet_workflow import SpreadsheetAnalysisWorkflow
from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider
from testagent_dev_agent.paths import REPOSITORY_ROOT


PROJECT_ROOT = REPOSITORY_ROOT
WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
FIXTURES_ROOT = PROJECT_ROOT / "fixtures"
ARTIFACT_ROOT = PROJECT_ROOT / "artifacts"
MAX_SPREADSHEET_BYTES = 10 * 1024 * 1024
Renderer = Callable[[dict[str, Any]], bytes]


def analyze_spreadsheet(
    path: str,
    *,
    generate_briefing: bool = False,
    workflow: Any | None = None,
    artifact_store: ArtifactStore | None = None,
    pdf_renderer: Renderer = render_pdf,
    pptx_renderer: Renderer = render_pptx,
) -> dict[str, Any]:
    """在一次性 AGSX Code 沙箱中分析表格，并返回产物和清理状态。"""
    try:
        source = _resolve_spreadsheet(path)
    except ValueError as exc:
        return _error("INVALID_SPREADSHEET_PATH", str(exc))
    if not source.exists():
        return _error("FILE_NOT_FOUND", f"表格不存在：{path}", retryable=True)
    if not source.is_file():
        return _error("NOT_A_FILE", f"表格路径不是文件：{path}")
    if source.stat().st_size > MAX_SPREADSHEET_BYTES:
        return _error("FILE_TOO_LARGE", f"表格超过 {MAX_SPREADSHEET_BYTES} bytes 限制。")

    store = artifact_store or ArtifactStore(ARTIFACT_ROOT)
    if workflow is None:
        load_dotenv(PROJECT_ROOT / ".env")
        try:
            config = TencentAGSXConfig.from_env()
        except RuntimeError as exc:
            return _error("CONFIGURATION_ERROR", str(exc))
        workflow = SpreadsheetAnalysisWorkflow(TencentAGSXProvider(config), store)

    attempt_id = _attempt_id()
    context = RequestContext(
        tenant_id="local",
        user_id="testagent",
        task_id="spreadsheet-analysis",
        attempt_id=attempt_id,
    )
    try:
        result = workflow.run(source, context, source_name=source.name)
        receipt_payload = json.loads(store.read_bytes(result.receipt))
    except Exception as exc:
        return _error(
            "SPREADSHEET_WORKFLOW_ERROR",
            f"{type(exc).__name__}: {exc}",
            retryable=True,
        )

    output_records = list(result.artifacts)
    render_error: str | None = None
    if result.success and generate_briefing and result.analysis is not None:
        try:
            output_records.append(
                store.put_bytes(
                    attempt_id,
                    "briefing.pdf",
                    pdf_renderer(result.analysis),
                    "application/pdf",
                )
            )
            output_records.append(
                store.put_bytes(
                    attempt_id,
                    "briefing.pptx",
                    pptx_renderer(result.analysis),
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                )
            )
        except Exception as exc:
            render_error = f"{type(exc).__name__}: {exc}"

    receipt_payload["outputs"] = [item.to_dict() for item in output_records]
    if render_error:
        receipt_payload.update({"status": "failed", "error": render_error})
    receipt_record = store.put_json(
        attempt_id,
        "execution-receipt.json",
        receipt_payload,
    )
    artifacts = [
        {**item.to_dict(), "path": f"artifacts/{item.relative_path}"}
        for item in output_records
    ]
    artifacts.append(
        {**receipt_record.to_dict(), "path": f"artifacts/{receipt_record.relative_path}"}
    )
    cleanup = receipt_payload.get("cleanup") or {}
    return {
        "success": result.success and render_error is None,
        "analysis": result.analysis,
        "briefing_requested": generate_briefing,
        "attempt_id": attempt_id,
        "provider": receipt_payload.get("provider"),
        "sandbox_id": receipt_payload.get("sandbox_id"),
        "sandbox_destroyed": bool(cleanup.get("destroyed")),
        "artifacts": artifacts,
        "receipt_path": f"artifacts/{receipt_record.relative_path}",
        "error": render_error or result.error,
    }


def generate_business_briefing(path: str, **kwargs: Any) -> dict[str, Any]:
    """分析表格并追加固定版式 PDF/PPTX 简报。"""
    return analyze_spreadsheet(path, generate_briefing=True, **kwargs)


def _resolve_spreadsheet(path: str) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path 必须是非空字符串。")
    requested = Path(path.strip())
    if requested.is_absolute():
        raise ValueError("只允许 workspace 或 fixtures 中的相对路径。")
    if requested.parts and requested.parts[0] in {"workspace", "fixtures"}:
        candidate = (PROJECT_ROOT / requested).resolve()
    else:
        candidate = (WORKSPACE_ROOT / requested).resolve()
    if not any(candidate.is_relative_to(root.resolve()) for root in (WORKSPACE_ROOT, FIXTURES_ROOT)):
        raise ValueError("禁止读取 workspace 或 fixtures 之外的文件。")
    if candidate.suffix.lower() not in {".csv", ".xlsx"}:
        raise ValueError("只接受 .csv 或 .xlsx 文件。")
    return candidate


def _attempt_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"agent-spreadsheet-{timestamp}-{uuid4().hex[:8]}"


def _error(error_type: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
        "sandbox_destroyed": False,
    }
