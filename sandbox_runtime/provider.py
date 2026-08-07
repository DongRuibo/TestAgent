"""业务工作流依赖的最小 Sandbox Provider 协议。"""

from __future__ import annotations

from typing import Protocol

from sandbox_runtime.models import (
    CleanupResult,
    ExecutionResult,
    ProviderCapabilities,
    RequestContext,
    SandboxSession,
)


def provider_name(provider: SandboxProvider) -> str:
    """取得 Receipt 使用的 Provider 名称，避免业务工作流硬编码厂商。"""
    explicit = getattr(provider, "provider_name", None)
    if explicit:
        return str(explicit)
    try:
        return str(provider.capabilities().provider)
    except (AttributeError, RuntimeError):
        return "unknown"


class SandboxProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    def create_code(self, context: RequestContext, ttl_seconds: int = 300) -> SandboxSession: ...

    def create_browser(
        self,
        context: RequestContext,
        ttl_seconds: int = 300,
    ) -> SandboxSession: ...

    def execute_code(
        self,
        session: SandboxSession,
        code: str,
        *,
        language: str = "python",
        timeout_seconds: float = 30,
    ) -> ExecutionResult: ...

    def put_file(self, session: SandboxSession, remote_path: str, data: bytes) -> None: ...

    def get_file(self, session: SandboxSession, remote_path: str) -> bytes: ...

    def browser_urls(
        self,
        session: SandboxSession,
    ) -> tuple[str, str, dict[str, str]]: ...

    def terminate(self, session: SandboxSession) -> CleanupResult: ...
