"""V2 腾讯云 AGSX 代码执行工具：每次调用使用一个独立沙箱实例。"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

from sandbox_runtime import ArtifactStore
from sandbox_runtime.models import CleanupResult, ExecutionReceipt, RequestContext

MAX_CODE_CHARS = 20_000
MAX_OUTPUT_CHARS = 20_000
DEFAULT_INSTANCE_TIMEOUT = 300
DEFAULT_EXECUTION_TIMEOUT = 30
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AGSXConfig:
    """创建 AGSX 实例所需的配置。"""

    api_key: str
    domain: str
    template: str

    @classmethod
    def from_env(cls) -> "AGSXConfig":
        """从环境变量读取配置，并在联网前报告缺失项或 Key 格式问题。"""
        values = {
            "E2B_API_KEY": os.getenv("E2B_API_KEY", "").strip(),
            "E2B_DOMAIN": os.getenv("E2B_DOMAIN", "").strip(),
            "AGSX_TEMPLATE": os.getenv("AGSX_TEMPLATE", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        if not values["E2B_API_KEY"].startswith("e2b_"):
            raise RuntimeError("E2B_API_KEY must use the e2b_ compatible format.")
        return cls(
            api_key=values["E2B_API_KEY"],
            domain=values["E2B_DOMAIN"],
            template=values["AGSX_TEMPLATE"],
        )


SandboxFactory = Callable[[AGSXConfig, int], Any]


def run_code(
    code: str,
    *,
    config: AGSXConfig | None = None,
    sandbox_factory: SandboxFactory | None = None,
    instance_timeout: int = DEFAULT_INSTANCE_TIMEOUT,
    execution_timeout: int = DEFAULT_EXECUTION_TIMEOUT,
    max_code_chars: int = MAX_CODE_CHARS,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> dict[str, Any]:
    """在一次性 AGSX 沙箱中执行 Python，并返回结构化结果。

    sandbox_factory 允许离线测试注入假沙箱；真实运行时才会导入并调用 E2B SDK。
    无论执行成功还是失败，只要实例创建成功，finally 都会尝试销毁它。
    """
    attempt_id = _new_attempt_id()
    if not isinstance(code, str) or not code.strip():
        return _rejected_with_receipt(
            attempt_id,
            "INVALID_CODE",
            "run_code 需要非空字符串参数 code。",
        )
    if len(code) > max_code_chars:
        return _rejected_with_receipt(
            attempt_id,
            "CODE_TOO_LARGE",
            f"代码长度 {len(code)} 超过限制 {max_code_chars}。",
        )

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        resolved_config = config or AGSXConfig.from_env()
    except RuntimeError as exc:
        return _rejected_with_receipt(attempt_id, "CONFIGURATION_ERROR", str(exc))

    factory = sandbox_factory or _create_sandbox
    context = RequestContext(
        tenant_id="demo-tenant",
        user_id="agent-cli-user",
        task_id="run-code",
        attempt_id=attempt_id,
    )
    receipt = ExecutionReceipt(
        receipt_id=f"receipt-{uuid4().hex}",
        context=context,
        provider="tencent-agsx",
        action="run_code",
        started_at=datetime.now(UTC),
    )
    sandbox: Any | None = None
    result: dict[str, Any] | None = None
    destroyed = False
    cleanup_error: str | None = None

    try:
        sandbox = factory(resolved_config, instance_timeout)
        receipt.sandbox_id = str(getattr(sandbox, "sandbox_id", "") or attempt_id)
        execution = sandbox.run_code(code, timeout=execution_timeout)
        stdout, stdout_truncated = _collect_output(
            execution.logs.stdout,
            max_output_chars,
        )
        stderr, stderr_truncated = _collect_output(
            execution.logs.stderr,
            max_output_chars,
        )

        if execution.error is not None:
            result = {
                "success": False,
                "error_type": "REMOTE_EXECUTION_ERROR",
                "message": str(execution.error),
                "retryable": False,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            }
        else:
            result = {
                "success": True,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "execution_count": execution.execution_count,
            }
    except Exception as exc:  # SDK、网络和超时异常都转换为工具观察结果。
        result = _error(
            "SANDBOX_EXECUTION_ERROR",
            _safe_exception_message(exc, resolved_config.api_key),
            retryable=True,
        )
    finally:
        if sandbox is not None:
            try:
                destroyed = bool(sandbox.kill())
                if not destroyed:
                    cleanup_error = "AGSX kill() returned false."
            except Exception as exc:
                cleanup_error = _safe_exception_message(exc, resolved_config.api_key)

            receipt.cleanup = CleanupResult(
                receipt.sandbox_id or attempt_id,
                destroyed,
                cleanup_error,
            )
        receipt.status = (
            "passed"
            if result is not None and result.get("success") and cleanup_error is None
            else "failed"
        )
        receipt.error = cleanup_error or (
            result.get("message") if result is not None and not result.get("success") else None
        )
        receipt.finished_at = datetime.now(UTC)

    if result is None:  # 防御性分支：正常控制流不会到这里。
        result = _error("SANDBOX_EXECUTION_ERROR", "沙箱没有返回执行结果。", retryable=True)

    result["sandbox_destroyed"] = destroyed
    if cleanup_error is not None:
        if result.get("success"):
            result.update(
                {
                    "success": False,
                    "error_type": "SANDBOX_CLEANUP_ERROR",
                    "message": cleanup_error,
                    "retryable": True,
                }
            )
        else:
            result["cleanup_error"] = cleanup_error
    try:
        receipt_record = ArtifactStore(PROJECT_ROOT / "artifacts").put_json(
            attempt_id,
            "execution-receipt.json",
            receipt.to_dict(),
        )
        result["attempt_id"] = attempt_id
        result["sandbox_id"] = receipt.sandbox_id
        result["receipt_path"] = f"artifacts/{receipt_record.relative_path}"
    except Exception as exc:
        # Receipt 写入失败不能隐藏原始执行结果，但必须显式暴露审计缺口。
        result["receipt_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _create_sandbox(config: AGSXConfig, timeout: int) -> Any:
    """延迟导入 SDK，并显式传入腾讯云 endpoint 与密钥。"""
    from e2b_code_interpreter import Sandbox

    return Sandbox.create(
        template=config.template,
        timeout=timeout,
        api_key=config.api_key,
        domain=config.domain,
    )


def _collect_output(lines: list[str], max_chars: int) -> tuple[str, bool]:
    """合并 SDK 日志并限制回填给模型的字符数。"""
    text = "".join(str(line) for line in lines)
    truncated = len(text) > max_chars
    return text[:max_chars], truncated


def _safe_exception_message(exc: Exception, api_key: str) -> str:
    """避免第三方 SDK 异常意外把密钥放进工具结果。"""
    message = str(exc)
    return message.replace(api_key, "[REDACTED]") if api_key else message


def _error(
    error_type: str,
    message: str,
    retryable: bool = False,
) -> dict[str, Any]:
    """构造与其他工具一致的结构化失败结果。"""
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
        "sandbox_destroyed": False,
    }


def _new_attempt_id() -> str:
    return f"agent-code-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"


def _rejected_with_receipt(
    attempt_id: str,
    error_type: str,
    message: str,
) -> dict[str, Any]:
    """为未创建沙箱的拒绝/配置失败也留下审计回执。"""
    result = _error(error_type, message)
    context = RequestContext(
        tenant_id="demo-tenant",
        user_id="agent-cli-user",
        task_id="run-code",
        attempt_id=attempt_id,
    )
    receipt = ExecutionReceipt(
        receipt_id=f"receipt-{uuid4().hex}",
        context=context,
        provider="tencent-agsx",
        action="run_code",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        status="failed",
        error=message,
    )
    try:
        record = ArtifactStore(PROJECT_ROOT / "artifacts").put_json(
            attempt_id,
            "execution-receipt.json",
            receipt.to_dict(),
        )
        result.update(
            {
                "attempt_id": attempt_id,
                "sandbox_id": None,
                "receipt_path": f"artifacts/{record.relative_path}",
            }
        )
    except Exception as exc:
        result["receipt_error"] = f"{type(exc).__name__}: {exc}"
    return result
