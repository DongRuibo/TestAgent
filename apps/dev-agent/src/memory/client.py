"""Memory Service 的同步客户端，兼容 Mem0-primary 与受治理 Context Pack。"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MEMORY_BASE_URL = "http://127.0.0.1:18081"
DEFAULT_MEMORY_TIMEOUT_SECONDS = 30.0
DEFAULT_MEMORY_LIMIT = 6
GOVERNED_TOKEN_BUDGET = 2048
GOVERNED_TOKENIZER_ID = "demo-conservative-char-v1"


class MemoryClientError(RuntimeError):
    """对外隐藏响应正文与认证信息的记忆客户端错误。"""


@dataclass(frozen=True)
class MemoryConfig:
    base_url: str
    timeout_seconds: float
    tenant_id: str
    user_id: str
    automatic_extraction: bool
    health_base_url: str | None = None
    backend_mode: str = "mem0_primary"
    bearer_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.backend_mode not in {"mem0_primary", "governed"}:
            raise MemoryClientError("无效的 Memory 后端模式。")
        if self.backend_mode == "governed":
            normalized_url = _normalize_base_url(self.base_url)
            object.__setattr__(self, "base_url", normalized_url)
            hostname = urlparse(normalized_url).hostname
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = hostname.lower() == "localhost"
            if not loopback and not self.bearer_token:
                raise MemoryClientError("非本机 governed Memory Service 必须提供 Bearer Token。")
            if self.bearer_token is not None and (
                not self.bearer_token
                or not self.bearer_token.isascii()
                or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in self.bearer_token)
            ):
                raise MemoryClientError("Memory Bearer Token 必须是非空可见 ASCII。")

    @classmethod
    def from_env(cls) -> "MemoryConfig | None":
        if not _boolean_env("MEMORY_ENABLED", default=False):
            return None

        backend_mode = os.getenv("MEMORY_BACKEND_MODE", "mem0_primary").strip()
        raw_base_url = os.getenv("MEMORY_BASE_URL", DEFAULT_MEMORY_BASE_URL).strip()
        base_url = raw_base_url if backend_mode == "governed" else _normalize_base_url(raw_base_url)
        health_base_url = os.getenv("MEMORY_HEALTH_BASE_URL", "").strip()
        if health_base_url:
            health_base_url = _normalize_base_url(
                health_base_url, "MEMORY_HEALTH_BASE_URL"
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

        bearer_token = os.getenv("MEMORY_BEARER_TOKEN", "").strip() or None
        return cls(
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            tenant_id=tenant_id,
            user_id=user_id,
            automatic_extraction=_boolean_env("MEMORY_AUTO_EXTRACT", default=True),
            health_base_url=health_base_url or None,
            backend_mode=backend_mode,
            bearer_token=bearer_token,
        )


@dataclass(frozen=True)
class MemoryContext:
    items: tuple[dict[str, Any], ...]
    context_pack_id: str | None = None

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
        # 新版 Memory 将健康检查与业务接口放在不同端口。
        self._request(
            "GET", "/health/ready", allow_empty=True,
            base_url=self.config.health_base_url,
        )
        return True

    def acquire_context(
        self,
        *,
        query: str,
        task_id: str | None = None,
        attempt_id: str | None = None,
        reference_time: datetime | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> MemoryContext:
        if self.config.backend_mode == "governed":
            return self._acquire_governed(query, task_id, attempt_id, reference_time, timezone)
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
        task_id: str | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> dict[str, int]:
        if not self.config.automatic_extraction:
            return {"memory_count": 0}
        UUID(episode_id)
        UUID(attempt_id)
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        if self.config.backend_mode == "governed":
            if task_id is None:
                raise MemoryClientError("governed Completed Turn 需要 task_id。")
            UUID(task_id)
            response = self._request(
                "POST", f"/v1/memory/episodes/{episode_id}/turns",
                json_body={
                    "turn_id": attempt_id, "task_id": task_id, "attempt_id": attempt_id,
                    "user_content": user_content, "assistant_content": assistant_content,
                    "verified_tool_results": verified_tool_results, "outcome": "succeeded",
                    "completed_at": completed_at.astimezone(UTC).isoformat(),
                    "timezone": timezone, "project_scope_selector": None,
                },
                idempotency_key=f"testagent-completed-turn-{attempt_id}",
            )
            extraction = response.get("per_turn_extraction")
            if (
                response.get("episode_id") != episode_id
                or response.get("turn_id") != attempt_id
                or type(response.get("sequence_no")) is not int
                or response["sequence_no"] < 1
                or not isinstance(extraction, dict)
                or type(extraction.get("candidate_count")) is not int
                or not isinstance(extraction.get("candidates"), list)
                or not 0 <= extraction["candidate_count"] <= 4
                or extraction["candidate_count"] != len(extraction["candidates"])
            ):
                raise MemoryClientError("Memory Service 返回了无效的 Completed Turn 回执。")
            return {"candidate_count": extraction["candidate_count"]}
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

    def _acquire_governed(
        self, query: str, task_id: str | None, attempt_id: str | None,
        reference_time: datetime | None, timezone: str,
    ) -> MemoryContext:
        if task_id is None or attempt_id is None:
            raise MemoryClientError("governed Context Pack 需要本轮 task_id 和 attempt_id。")
        task_context = {"task_id": str(UUID(task_id)), "attempt_id": str(UUID(attempt_id)),
                        "purpose": "answer_user"}
        if reference_time is not None and reference_time.tzinfo is None:
            raise MemoryClientError("Context Pack reference_time 必须包含时区。")
        manifest = self._request(
            "POST", "/v1/memory/context-packs",
            json_body={
                "task_context": task_context, "query": query,
                "scope_selectors": [{"kind": "user", "id": self.config.user_id}],
                "memory_types": ["preference", "profile_fact", "semantic_fact", "episodic_summary"],
                "limit": DEFAULT_MEMORY_LIMIT,
                "token_budget": GOVERNED_TOKEN_BUDGET, "tokenizer_id": GOVERNED_TOKENIZER_ID,
                "memory_required": False, "pinned_memory_version_ids": [],
                "reference_time": reference_time.astimezone(UTC).isoformat() if reference_time else None,
                "timezone": timezone,
            },
        )
        # 两个 HTTP 响应各在入口解析一次，正文只能来自通过绑定核验的 Consume。
        try:
            metadata = _pack_metadata(manifest)
            count = manifest["item_count"]
            if (
                any(metadata[name] != value for name, value in task_context.items())
                or metadata["tokenizer_id"] != GOVERNED_TOKENIZER_ID
                or metadata["token_budget"] != GOVERNED_TOKEN_BUDGET
                or metadata["timezone"] != timezone
                or (reference_time is not None and metadata["reference_time"] != reference_time.astimezone(UTC))
                or type(count) is not int or count < 0
                or manifest["delivery_status"] not in {"compiled", "consumed"}
                or (manifest.get("consumed_at") is not None and not (
                    metadata["created_at"] <= _timestamp(manifest["consumed_at"]) < metadata["expires_at"]
                ))
            ):
                raise ValueError("manifest binding")
        except (ValueError, TypeError, KeyError, AttributeError, ZoneInfoNotFoundError) as exc:
            raise MemoryClientError("Memory Context Pack Manifest 契约不匹配。") from exc
        pack = self._request(
            "POST", f"/v1/memory/context-packs/{metadata['context_pack_id']}/consume",
            json_body={"task_context": task_context},
            idempotency_key=f"testagent-consume-{attempt_id}",
        )
        try:
            delivered = _pack_metadata(pack)
            if delivered != metadata or not (
                metadata["created_at"] <= _timestamp(pack["consumed_at"]) < metadata["expires_at"]
            ):
                raise ValueError("consume binding")
            sections = pack["sections"]
            if not isinstance(sections, list):
                raise ValueError("sections")
            items = []
            for section in sections:
                if not isinstance(section["kind"], str) or not section["kind"].strip() or not isinstance(section["items"], list):
                    raise ValueError("section")
                for item in section["items"]:
                    record_id = str(UUID(item["memory_record_id"]))
                    version_id = str(UUID(item["memory_version_id"]))
                    if (
                        not isinstance(item["text"], str) or not item["text"].strip()
                        or item["trust"] != "untrusted_memory_data"
                        or not isinstance(item["source_refs"], list) or not item["source_refs"]
                        or not all(isinstance(ref, str) and ref.strip() for ref in item["source_refs"])
                        or not isinstance(item["inclusion_reason"], str) or not item["inclusion_reason"].strip()
                    ):
                        raise ValueError("item provenance")
                    if item.get("effective_from") is not None:
                        _timestamp(item["effective_from"])
                    event_time = item.get("event_time")
                    if event_time is not None:
                        if (
                            _timestamp(event_time["start"]) >= _timestamp(event_time["end"])
                            or event_time["precision"] not in {
                                "instant", "minute", "hour", "part_of_day", "day", "week", "month", "quarter", "year"
                            }
                        ):
                            raise ValueError("event time")
                        _timezone(event_time["timezone"])
                    items.append({
                        "kind": section["kind"], "text": item["text"], "memory_id": record_id,
                        "memory_version_id": version_id, "source_refs": item["source_refs"],
                        "effective_from": item.get("effective_from"), "event_time": event_time,
                        "inclusion_reason": item["inclusion_reason"], "trust": item["trust"],
                    })
            if len(items) != count:
                raise ValueError("item count")
        except (ValueError, TypeError, KeyError, AttributeError, ZoneInfoNotFoundError) as exc:
            raise MemoryClientError("Memory Context Pack Consume 契约不匹配。") from exc
        return MemoryContext(tuple(items), metadata["context_pack_id"])

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
        base_url: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "X-Tenant-Id": self.config.tenant_id,
            "X-User-Id": self.config.user_id,
        }
        # 独立健康地址不接收业务 Bearer，避免把身份令牌转发给其他服务。
        if self.config.backend_mode == "governed" and self.config.bearer_token and base_url is None:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._client.request(
                method,
                f"{base_url or self.config.base_url}{path}",
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


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp timezone")
    return parsed.astimezone(UTC)


def _timezone(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 64 or value.strip() != value:
        raise ValueError("timezone")
    ZoneInfo(value)


def _pack_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = {name: str(UUID(payload[name])) for name in ("context_pack_id", "task_id", "attempt_id")}
    for name in ("purpose", "authority_snapshot_id", "policy_version", "compiler_version",
                 "ranking_version", "projection_generation", "tokenizer_id", "timezone"):
        value = payload[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(name)
        metadata[name] = value
    _timezone(metadata["timezone"])
    for name in ("reference_time", "created_at", "expires_at"):
        metadata[name] = _timestamp(payload[name])
    for name in ("token_budget", "estimated_tokens"):
        if type(payload[name]) is not int:
            raise ValueError(name)
        metadata[name] = payload[name]
    summary = payload["omission_summary"]
    if not isinstance(summary, dict) or not all(
        isinstance(key, str) and key.strip() and type(value) is int and value >= 0
        for key, value in summary.items()
    ):
        raise ValueError("omission summary")
    metadata["omission_summary"] = summary
    if not (
        metadata["created_at"] < metadata["expires_at"]
        and datetime.now(UTC) < metadata["expires_at"]
        and 0 <= metadata["estimated_tokens"] <= metadata["token_budget"]
    ):
        raise ValueError("pack validity")
    return metadata


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


def _normalize_base_url(raw_url: str, name: str = "MEMORY_BASE_URL") -> str:
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MemoryClientError(f"{name} 必须是有效的 HTTP(S) 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MemoryClientError(f"{name} 不能包含凭据、查询参数或片段。")
    if parsed.path not in {"", "/"}:
        raise MemoryClientError(f"{name} 不能包含路径。")
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
