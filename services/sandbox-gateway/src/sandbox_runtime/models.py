"""Provider、Artifact 和执行回执共用的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，避免本地时区导致审计歧义。"""
    return datetime.now(UTC)


class SandboxKind(StrEnum):
    CODE = "code"
    BROWSER = "browser"


@dataclass(frozen=True)
class RequestContext:
    tenant_id: str
    user_id: str
    task_id: str
    attempt_id: str

    def metadata(self) -> dict[str, str]:
        return {
            "managed_by": "lazyx-testagent",
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
        }


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    kinds: tuple[SandboxKind, ...]
    languages: tuple[str, ...]
    network_modes: tuple[str, ...]
    supports_file_transfer: bool
    supports_pause: bool
    supports_domain_egress_policy: bool
    max_ttl_seconds: int | None = None


@dataclass
class SandboxSession:
    sandbox_id: str
    provider: str
    kind: SandboxKind
    template: str
    created_at: datetime
    expires_at: datetime
    metadata: dict[str, str]
    native: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    error_type: str | None = None
    error_message: str | None = None
    execution_count: int | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "execution_count": self.execution_count,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class CleanupResult:
    sandbox_id: str
    destroyed: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sandbox_id": self.sandbox_id,
            "destroyed": self.destroyed,
            "error": self.error,
        }


@dataclass(frozen=True)
class ArtifactRecord:
    name: str
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }


@dataclass
class ExecutionReceipt:
    receipt_id: str
    context: RequestContext
    provider: str
    action: str
    started_at: datetime
    finished_at: datetime | None = None
    sandbox_id: str | None = None
    status: str = "running"
    inputs: list[ArtifactRecord] = field(default_factory=list)
    outputs: list[ArtifactRecord] = field(default_factory=list)
    network_destinations: list[str] = field(default_factory=list)
    cleanup: CleanupResult | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "tenant_id": self.context.tenant_id,
            "user_id": self.context.user_id,
            "task_id": self.context.task_id,
            "attempt_id": self.context.attempt_id,
            "provider": self.provider,
            "sandbox_id": self.sandbox_id,
            "action": self.action,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "status": self.status,
            "inputs": [item.to_dict() for item in self.inputs],
            "outputs": [item.to_dict() for item in self.outputs],
            "network_destinations": list(self.network_destinations),
            "cleanup": self.cleanup.to_dict() if self.cleanup else None,
            "error": self.error,
        }
