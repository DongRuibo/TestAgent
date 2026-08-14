"""Agent 工具：使用一次性 AGSX Browser Sandbox 读取公开网页。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

from sandbox_runtime import ArtifactStore, RequestContext
from sandbox_runtime.browser_security import resolve_public_url, validate_public_url
from sandbox_runtime.browser_workflow import PublicBrowserWorkflow
from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider
from testagent_dev_agent.paths import REPOSITORY_ROOT


PROJECT_ROOT = REPOSITORY_ROOT
ARTIFACT_ROOT = PROJECT_ROOT / "artifacts"
MAX_URL_CHARS = 2_048


def browse_public_page(
    url: str,
    *,
    workflow: Any | None = None,
) -> dict[str, Any]:
    """提取一个公开网页，并返回不可信正文预览、截图和清理状态。"""
    if not isinstance(url, str) or not url.strip():
        return _error("INVALID_URL", "url 必须是非空字符串。")
    url = url.strip()
    if len(url) > MAX_URL_CHARS:
        return _error("INVALID_URL", f"URL 长度超过 {MAX_URL_CHARS} 字符。")
    try:
        validate_public_url(url)
    except ValueError as exc:
        return _error("FORBIDDEN_URL", str(exc))

    resolved_addresses: tuple[str, ...] = ()
    if workflow is None:
        load_dotenv(PROJECT_ROOT / ".env")
        try:
            resolved_addresses = resolve_public_url(url)
        except ValueError as exc:
            return _error("FORBIDDEN_DNS_TARGET", str(exc))
        try:
            config = TencentAGSXConfig.from_env(
                require_browser=True,
                default_browser_template="lazyx-browser-public-poc",
            )
        except RuntimeError as exc:
            return _error("CONFIGURATION_ERROR", str(exc))
        store = ArtifactStore(ARTIFACT_ROOT)
        workflow = PublicBrowserWorkflow(TencentAGSXProvider(config), store)

    attempt_id = _attempt_id("agent-browser")
    context = RequestContext(
        tenant_id="demo-tenant",
        user_id="agent-cli-user",
        task_id="public-web-extract",
        attempt_id=attempt_id,
    )
    try:
        result = workflow.run(url, context)
    except Exception as exc:
        return _error("BROWSER_WORKFLOW_ERROR", f"{type(exc).__name__}: {exc}", retryable=True)

    return {
        "success": result.success,
        "attempt_id": attempt_id,
        "source_url": result.source_url,
        "resolved_addresses": list(resolved_addresses),
        "dns_rebinding_protection": "single_preflight_only",
        "final_url": result.final_url,
        "status_code": result.status_code,
        "title": result.title,
        "content": result.text_preview,
        "content_total_chars": result.text_total_chars,
        "screenshot": {
            "page_height": result.page_height,
            "captured_height": result.screenshot_height,
            "full_page": result.screenshot_full_page,
            "truncated": result.screenshot_truncated,
            "stitched": result.screenshot_stitched,
            "scroll_steps": result.screenshot_scroll_steps,
        },
        "trust": result.trust,
        "instructions_executable": result.instructions_executable,
        "artifacts": [
            {
                **record.to_dict(),
                "path": f"artifacts/{record.relative_path}",
            }
            for record in result.artifacts
        ],
        "receipt_path": f"artifacts/{result.receipt.relative_path}",
        "sandbox_destroyed": bool(result.cleanup and result.cleanup.destroyed),
        "error": result.error,
    }


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
