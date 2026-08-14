"""Agent 使用 Sandbox Gateway 的统一工具入口，不直接持有 Provider 密钥。"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from sandbox_client import SandboxClient
from sandbox_contracts import ExecutionStatus, ExecutionView
from testagent_dev_agent.paths import REPOSITORY_ROOT


PROJECT_ROOT = REPOSITORY_ROOT
WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
FIXTURES_ROOT = PROJECT_ROOT / "fixtures"
MAX_SPREADSHEET_BYTES = 10 * 1024 * 1024


def run_code(code: str, *, client: SandboxClient | None = None) -> dict[str, Any]:
    if not isinstance(code, str) or not code.strip():
        return _error("INVALID_CODE", "run_code 需要非空字符串参数 code。")
    return _call(client, lambda gateway: gateway.execute_code(code, language="python"))


def analyze_spreadsheet(
    path: str,
    *,
    generate_briefing: bool = False,
    client: SandboxClient | None = None,
) -> dict[str, Any]:
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
    media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    return _call(
        client,
        lambda gateway: gateway.analyze_spreadsheet_file(
            source.name,
            source.read_bytes(),
            media_type=media_type,
            generate_briefing=generate_briefing,
            network_profile="provider_default",
        ),
    )


def generate_business_briefing(
    path: str,
    *,
    client: SandboxClient | None = None,
) -> dict[str, Any]:
    return analyze_spreadsheet(path, generate_briefing=True, client=client)


def browse_public_page(
    url: str,
    *,
    client: SandboxClient | None = None,
) -> dict[str, Any]:
    if not isinstance(url, str) or not url.strip():
        return _error("INVALID_URL", "url 必须是非空字符串。")
    return _call(client, lambda gateway: gateway.browse_public_page(url.strip()))


def _call(client: SandboxClient | None, operation: Any) -> dict[str, Any]:
    owned = client is None
    try:
        gateway = client or _build_client()
        view = operation(gateway)
        return _view_result(view)
    except Exception as exc:
        return _error("GATEWAY_REQUEST_ERROR", f"{type(exc).__name__}: {exc}", retryable=True)
    finally:
        if owned and "gateway" in locals():
            gateway.close()


def _build_client() -> SandboxClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SANDBOX_GATEWAY_URL", "").strip()
    api_key = os.getenv("SANDBOX_GATEWAY_API_KEY", "").strip()
    missing = [name for name, value in {
        "SANDBOX_GATEWAY_URL": base_url,
        "SANDBOX_GATEWAY_API_KEY": api_key,
    }.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return SandboxClient(base_url, api_key)


def _view_result(view: ExecutionView) -> dict[str, Any]:
    payload = dict(view.result or {})
    cleanup = (view.receipt or {}).get("cleanup") or {}
    payload.update({
        "success": view.status is ExecutionStatus.SUCCEEDED and bool(payload.get("success", True)),
        "execution_id": view.execution_id,
        "status": view.status.value,
        "provider": view.provider,
        "sandbox_id": view.sandbox_id,
        "sandbox_destroyed": bool(cleanup.get("destroyed")),
        "artifacts": [item.model_dump(mode="json") for item in view.artifacts],
        "error_type": view.error_type or payload.get("error_type"),
        "error": view.error_message or payload.get("error"),
    })
    return payload


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


def _error(error_type: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
        "sandbox_destroyed": False,
    }
