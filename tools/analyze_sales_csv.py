"""Agent 工具：把允许目录中的销售 CSV 交给 AGSX 分析。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

from sandbox_runtime import ArtifactStore, RequestContext
from sandbox_runtime.sales_workflow import SalesAnalysisWorkflow
from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
FIXTURES_ROOT = PROJECT_ROOT / "fixtures"
ARTIFACT_ROOT = PROJECT_ROOT / "artifacts"
MAX_SALES_CSV_BYTES = 5 * 1024 * 1024


def analyze_sales_csv(
    path: str,
    *,
    workflow: Any | None = None,
    artifact_store: ArtifactStore | None = None,
) -> dict[str, Any]:
    """分析销售 CSV，并返回摘要、Artifact、Receipt 与清理状态。"""
    try:
        csv_path = _resolve_sales_csv(path)
    except ValueError as exc:
        return _error("INVALID_SALES_PATH", str(exc))

    if not csv_path.exists():
        return _error("FILE_NOT_FOUND", f"销售 CSV 不存在：{path}", retryable=True)
    if not csv_path.is_file():
        return _error("NOT_A_FILE", f"销售 CSV 路径不是文件：{path}")
    if csv_path.suffix.lower() != ".csv":
        return _error("INVALID_FILE_TYPE", "销售分析目前只接受 .csv 文件。")
    if csv_path.stat().st_size > MAX_SALES_CSV_BYTES:
        return _error(
            "FILE_TOO_LARGE",
            f"销售 CSV 超过 {MAX_SALES_CSV_BYTES} bytes 限制。",
        )

    store = artifact_store or ArtifactStore(ARTIFACT_ROOT)
    if workflow is None:
        load_dotenv(PROJECT_ROOT / ".env")
        try:
            config = TencentAGSXConfig.from_env()
        except RuntimeError as exc:
            return _error("CONFIGURATION_ERROR", str(exc))
        workflow = SalesAnalysisWorkflow(TencentAGSXProvider(config), store)

    attempt_id = _attempt_id("agent-sales")
    context = RequestContext(
        tenant_id="demo-tenant",
        user_id="agent-cli-user",
        task_id="sales-analysis",
        attempt_id=attempt_id,
    )
    try:
        result = workflow.run(csv_path, context)
        receipt_payload = json.loads(store.read_bytes(result.receipt))
    except Exception as exc:
        return _error("SALES_WORKFLOW_ERROR", f"{type(exc).__name__}: {exc}", retryable=True)

    return {
        "success": result.success,
        "attempt_id": attempt_id,
        "summary": result.summary,
        "artifacts": [
            {
                **record.to_dict(),
                "path": f"artifacts/{record.relative_path}",
            }
            for record in result.artifacts
        ],
        "receipt_path": f"artifacts/{result.receipt.relative_path}",
        "sandbox_id": receipt_payload.get("sandbox_id"),
        "sandbox_destroyed": bool(
            (receipt_payload.get("cleanup") or {}).get("destroyed")
        ),
        "error": result.error,
    }


def _resolve_sales_csv(path: str) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path 必须是非空字符串。")
    requested = Path(path.strip())
    if requested.is_absolute():
        raise ValueError("只允许 workspace 或 fixtures 中的相对路径。")

    if requested.parts and requested.parts[0] in {"workspace", "fixtures"}:
        candidate = (PROJECT_ROOT / requested).resolve()
    else:
        candidate = (WORKSPACE_ROOT / requested).resolve()

    allowed_roots = (WORKSPACE_ROOT.resolve(), FIXTURES_ROOT.resolve())
    if not any(candidate.is_relative_to(root) for root in allowed_roots):
        raise ValueError("禁止读取 workspace 或 fixtures 之外的文件。")
    return candidate


def _attempt_id(prefix: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{timestamp}-{uuid4().hex[:8]}"


def _error(error_type: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
        "sandbox_destroyed": False,
    }
