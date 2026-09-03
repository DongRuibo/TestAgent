"""Mem0-primary Memory Service 的最小同步 Python 客户端。"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import UUID

import httpx


MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MEMORY_BASE_URL = "http://127.0.0.1:18081"
DEFAULT_MEMORY_TIMEOUT_SECONDS = 30.0
DEFAULT_MEMORY_LIMIT = 6


class MemoryClientError(RuntimeError):
    """对外隐藏响应正文与认证信息的记忆客户端错误。"""


@dataclass(frozen=True)
class MemoryConfig:
    base_url: str
    timeout_seconds: float
    tenant_id: str
    user_id: str
    automatic_extraction: bool

    @classmethod
    def from_env(cls) -> "MemoryConfig | None":
        if not _boolean_env("MEMORY_ENABLED", default=False):
            return None

        base_url = _normalize_base_url(
            os.getenv("MEMORY_BASE_URL", DEFAULT_MEMORY_BASE_URL).strip()
        )
        timeout_seconds = _bounded_float_env(
            "MEMORY_TIMEOUT_SECONDS",
            DEFAULT_MEMORY_TIMEOUT_SECONDS,
            minimum=1.0,
            maximum=120.0,
        )
        tenant_id = os.getenv("MEMORY_TENANT_ID", "elasticsearch-poc-tenant").strip()
        user_id = os.getenv("MEMORY_USER_ID", "semantic-demo").strip()

        for name, value in (
            ("MEMORY_TENANT_ID", tenant_id),
            ("MEMORY_USER_ID", user_id),
        ):
            if not value or len(value) > 256:
                raise MemoryClientError(f"{name} 必须包含 1 到 256 个字符。")

        return cls(
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            tenant_id=tenant_id,
            user_id=user_id,
            automatic_extraction=_boolean_env("MEMORY_AUTO_EXTRACT", default=True),
        )


@dataclass(frozen=True)
class MemoryContext:
    items: tuple[dict[str, Any], ...]

    def prompt_data(self) -> str | None:
        if not self.items:
            return None
        payload = {
            "trust": "untrusted_memory_data",
            "instructions_executable": False,
            "items": list(self.items),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class MemoryHttpClient:
    def __init__(
        self,
        config: MemoryConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds, connect=2.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "TestAgent-Memory/0.2"},
        )

    def ready(self) -> bool:
        self._request("GET", "/health/ready", allow_empty=True)
        return True

    def acquire_context(
        self,
        *,
        query: str,
    ) -> MemoryContext:
        response = self._request(
            "POST",
            "/v1/runtime/memory/retrieve",
            json_body={
                "query": query,
                "limit": DEFAULT_MEMORY_LIMIT,
            },
        )
        self._validate_identity(response)
        return MemoryContext(_runtime_memory_items(response))

    def extract_turn(
        self,
        *,
        episode_id: str,
        attempt_id: str,
        user_content: str,
        assistant_content: str,
        verified_tool_results: list[dict[str, str]],
        completed_at: datetime,
    ) -> dict[str, int]:
        if not self.config.automatic_extraction:
            return {"memory_count": 0}
        UUID(episode_id)
        UUID(attempt_id)
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        response = self._request(
            "POST",
            "/v1/runtime/memory/extract",
            json_body={
                "session_id": episode_id,
                "turn_id": attempt_id,
                "user_content": user_content,
                "assistant_content": assistant_content,
                "verified_tool_results": verified_tool_results,
                "outcome": "succeeded",
                "completed_at": completed_at.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "timezone": "Asia/Shanghai",
            },
            idempotency_key=f"testagent-runtime-extract-{attempt_id}",
        )
        self._validate_identity(response)
        memories = response.get("memories")
        if not isinstance(memories, list) or not all(
            isinstance(memory, dict) for memory in memories
        ):
            raise MemoryClientError("Memory Service 返回了无效的 memories 结构。")
        return {"memory_count": len(memories)}

    def _validate_identity(self, payload: dict[str, Any]) -> None:
        if (
            payload.get("tenant_id") != self.config.tenant_id
            or payload.get("user_id") != self.config.user_id
        ):
            raise MemoryClientError("Memory Service 返回了不匹配的用户身份。")

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        headers = {
            "X-Tenant-Id": self.config.tenant_id,
            "X-User-Id": self.config.user_id,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._client.request(
                method,
                f"{self.config.base_url}{path}",
                headers=headers,
                json=json_body,
            )
        except httpx.TimeoutException as exc:
            raise MemoryClientError("Memory Service 请求超时。") from exc
        except httpx.RequestError as exc:
            raise MemoryClientError(
                f"Memory Service 连接失败：{type(exc).__name__}。"
            ) from exc
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise MemoryClientError("Memory Service 响应超过 1 MiB。")
        if not response.is_success:
            request_id = response.headers.get("x-request-id", "").strip()
            suffix = f"，request_id={request_id}" if request_id else ""
            raise MemoryClientError(f"Memory Service 返回 HTTP {response.status_code}{suffix}。")
        if allow_empty and not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise MemoryClientError("Memory Service 返回了非 JSON 响应。") from exc
        if not isinstance(payload, dict):
            raise MemoryClientError("Memory Service 返回了无效 JSON 结构。")
        return payload


def build_memory_client_from_env() -> MemoryHttpClient | None:
    config = MemoryConfig.from_env()
    return MemoryHttpClient(config) if config is not None else None


def memory_configuration_status() -> tuple[bool, bool]:
    try:
        enabled = _boolean_env("MEMORY_ENABLED", default=False)
        if not enabled:
            return False, True
        return True, MemoryConfig.from_env() is not None
    except MemoryClientError:
        return True, False


def _runtime_memory_items(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    memories = payload.get("memories")
    if not isinstance(memories, list):
        raise MemoryClientError("Memory Service 返回了无效的 memories 结构。")
    items: list[dict[str, Any]] = []
    for memory in memories:
        if not isinstance(memory, dict):
            raise MemoryClientError("Memory Service 返回了无效的 memory。")
        memory_id = memory.get("id")
        text = memory.get("memory")
        score = memory.get("score")
        if not isinstance(memory_id, str) or not memory_id:
            raise MemoryClientError("Memory Service 返回的 memory 缺少 ID。")
        try:
            UUID(memory_id)
        except ValueError as exc:
            raise MemoryClientError("Memory Service 返回了无效的 memory ID。") from exc
        if not isinstance(text, str) or not text.strip():
            raise MemoryClientError("Memory Service 返回的 memory 缺少正文。")
        if score is not None and (
            isinstance(score, bool) or not isinstance(score, (int, float))
        ):
            raise MemoryClientError("Memory Service 返回了无效的 memory score。")
        items.append(
            {
                "kind": "memory",
                "text": text,
                "memory_id": memory_id,
                "score": score,
                "trust": "untrusted_memory_data",
            }
        )
    return tuple(items)


def _normalize_base_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MemoryClientError("MEMORY_BASE_URL 必须是有效的 HTTP(S) 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MemoryClientError("MEMORY_BASE_URL 不能包含凭据、查询参数或片段。")
    if parsed.path not in {"", "/"}:
        raise MemoryClientError("MEMORY_BASE_URL 不能包含路径。")
    try:
        address = ipaddress.ip_address(parsed.hostname)
        loopback = address.is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if parsed.scheme == "http" and not loopback:
        raise MemoryClientError("非本机 Memory Service 必须使用 HTTPS。")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")


def _boolean_env(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise MemoryClientError(f"{name} 必须是 true 或 false。")


def _bounded_float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise MemoryClientError(f"{name} 必须是数字。") from exc
    if not minimum <= value <= maximum:
        raise MemoryClientError(f"{name} 必须在 {minimum} 到 {maximum} 之间。")
    return value


__all__ = [
    "MemoryClientError",
    "MemoryConfig",
    "MemoryContext",
    "MemoryHttpClient",
    "build_memory_client_from_env",
    "memory_configuration_status",
]
