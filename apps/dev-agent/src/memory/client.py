"""LazyTT Memory Service 的最小同步 Python 客户端。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import UUID

import httpx
from dotenv import dotenv_values


MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MEMORY_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_MEMORY_TIMEOUT_SECONDS = 30.0
DEFAULT_PROJECTION_TIMEOUT_SECONDS = 8.0
DEFAULT_TOKEN_BUDGET = 1024
DEFAULT_MEMORY_LIMIT = 6
DEFAULT_TOKENIZER_ID = "demo-conservative-char-v1"
DEFAULT_MEMORY_TYPES = (
    "preference",
    "profile_fact",
    "semantic_fact",
    "episodic_summary",
)


class MemoryClientError(RuntimeError):
    """对外隐藏响应正文与认证信息的记忆客户端错误。"""


@dataclass(frozen=True)
class MemoryConfig:
    base_url: str
    timeout_seconds: float
    projection_timeout_seconds: float
    tenant_id: str
    user_id: str
    project_id: str | None
    bearer_token: str | None
    jwt_secret: str | None
    jwt_issuer: str
    jwt_audience: str
    automatic_extraction: bool
    episode_consolidation: bool

    @classmethod
    def from_env(cls) -> "MemoryConfig | None":
        if not _boolean_env("MEMORY_ENABLED", default=False):
            return None

        local_values = _load_memory_env_file(os.getenv("MEMORY_ENV_FILE", "").strip())

        def configured(name: str, default: str = "") -> str:
            value = os.getenv(name)
            if value is None:
                value = local_values.get(name, default)
            return str(value or "").strip()

        base_url = _normalize_base_url(configured("MEMORY_BASE_URL", DEFAULT_MEMORY_BASE_URL))
        timeout_seconds = _bounded_float_env(
            "MEMORY_TIMEOUT_SECONDS",
            DEFAULT_MEMORY_TIMEOUT_SECONDS,
            minimum=1.0,
            maximum=120.0,
        )
        projection_timeout_seconds = _bounded_float_env(
            "MEMORY_PROJECTION_TIMEOUT_SECONDS",
            DEFAULT_PROJECTION_TIMEOUT_SECONDS,
            minimum=0.0,
            maximum=60.0,
        )
        tenant_id = configured("MEMORY_TENANT_ID", configured("DEMO_TENANT_ID", "demo-tenant"))
        user_id = configured("MEMORY_USER_ID", configured("DEMO_USER_ID", "semantic-demo"))
        project_id = configured(
            "MEMORY_PROJECT_ID",
            configured("DEMO_PROJECT_ID", "lazytt-memory-system-demo"),
        ) or None
        issuer = configured("MEMORY_JWT_ISSUER", "lazytt-demo-web-api")
        audience = configured("MEMORY_JWT_AUDIENCE", "lazytt-memory-service")
        bearer_token = configured("MEMORY_BEARER_TOKEN") or None
        jwt_secret = configured("MEMORY_JWT_HS256_SECRET") or None

        for name, value in (
            ("MEMORY_TENANT_ID", tenant_id),
            ("MEMORY_USER_ID", user_id),
            ("MEMORY_JWT_ISSUER", issuer),
            ("MEMORY_JWT_AUDIENCE", audience),
        ):
            if not value or len(value) > 256:
                raise MemoryClientError(f"{name} 必须包含 1 到 256 个字符。")
        if bearer_token is None and jwt_secret is None:
            raise MemoryClientError(
                "MEMORY_ENABLED=true 时需要 MEMORY_BEARER_TOKEN、"
                "MEMORY_JWT_HS256_SECRET 或包含该 Secret 的 MEMORY_ENV_FILE。"
            )
        if jwt_secret is not None and len(jwt_secret.encode("utf-8")) < 32:
            raise MemoryClientError("MEMORY_JWT_HS256_SECRET 至少需要 32 字节。")

        return cls(
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            projection_timeout_seconds=projection_timeout_seconds,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            bearer_token=bearer_token,
            jwt_secret=jwt_secret,
            jwt_issuer=issuer,
            jwt_audience=audience,
            automatic_extraction=_boolean_env("MEMORY_AUTO_EXTRACT", default=True),
            episode_consolidation=_boolean_env(
                "MEMORY_EPISODE_CONSOLIDATION_ENABLED", default=False
            ),
        )


@dataclass(frozen=True)
class MemoryContext:
    context_pack_id: str
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
        clock: Any = time.time,
    ) -> None:
        self.config = config
        self._clock = clock
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds, connect=2.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "TestAgent-Memory/0.1"},
        )

    def ready(self) -> bool:
        self._request("GET", "/health/ready", allow_empty=True)
        return True

    @property
    def episode_consolidation_enabled(self) -> bool:
        return self.config.episode_consolidation

    def acquire_context(
        self,
        *,
        task_id: str,
        attempt_id: str,
        query: str,
    ) -> MemoryContext:
        task_context = _task_context(task_id, attempt_id)
        scope_selectors = [{"kind": "user", "id": self.config.user_id}]
        if self.config.project_id:
            scope_selectors.append({"kind": "project", "id": self.config.project_id})
        manifest = self._request(
            "POST",
            "/v1/memory/context-packs",
            json_body={
                "task_context": task_context,
                "query": query,
                "timezone": "Asia/Shanghai",
                "scope_selectors": scope_selectors,
                "memory_types": list(DEFAULT_MEMORY_TYPES),
                "limit": DEFAULT_MEMORY_LIMIT,
                "token_budget": DEFAULT_TOKEN_BUDGET,
                "tokenizer_id": DEFAULT_TOKENIZER_ID,
                "memory_required": False,
                "pinned_memory_version_ids": [],
            },
        )
        context_pack_id = _required_uuid(manifest, "context_pack_id")
        _validate_task_binding(manifest, task_id, attempt_id)
        pack = self._request(
            "POST",
            f"/v1/memory/context-packs/{context_pack_id}/consume",
            json_body={"task_context": task_context},
            idempotency_key=f"testagent-consume-{attempt_id}",
        )
        _validate_task_binding(pack, task_id, attempt_id)
        if pack.get("context_pack_id") != context_pack_id:
            raise MemoryClientError("Memory Service 返回了不匹配的 Context Pack。")
        return MemoryContext(context_pack_id, _context_items(pack))

    def extract_turn(
        self,
        *,
        task_id: str,
        attempt_id: str,
        content: str,
        occurred_at: datetime,
    ) -> dict[str, int]:
        if not self.config.automatic_extraction:
            return {"candidate_count": 0, "ready_count": 0, "pending_count": 0}
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        request_body: dict[str, Any] = {
            "event_id": f"testagent-event-{attempt_id}",
            "source_type": "conversation_message",
            "source_ref": f"testagent:{task_id}:attempt:{attempt_id}:user",
            "content": content,
            "occurred_at": occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "timezone": "Asia/Shanghai",
            "max_candidates": 4,
            "scope_selector": {"kind": "user", "id": self.config.user_id},
        }
        if self.config.project_id:
            request_body["project_scope_selector"] = {
                "kind": "project",
                "id": self.config.project_id,
            }
        response = self._request(
            "POST",
            "/v1/memory/extractions",
            json_body=request_body,
            idempotency_key=f"testagent-extract-{attempt_id}",
        )
        candidates = response.get("candidates")
        if not isinstance(candidates, list):
            raise MemoryClientError("Memory Service 返回了无效的 extraction 结构。")
        return self._projection_summary(candidates)

    def record_completed_turn(
        self,
        *,
        episode_id: str,
        task_id: str,
        attempt_id: str,
        user_content: str,
        assistant_content: str,
        verified_tool_results: list[dict[str, str]],
        completed_at: datetime,
    ) -> dict[str, Any]:
        if not self.config.episode_consolidation:
            raise MemoryClientError("Episode consolidation feature flag is disabled.")
        UUID(episode_id)
        UUID(task_id)
        UUID(attempt_id)
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        request_body: dict[str, Any] = {
            "turn_id": attempt_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "user_content": user_content,
            "assistant_content": assistant_content,
            "verified_tool_results": verified_tool_results,
            "outcome": "succeeded",
            "completed_at": completed_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "timezone": "Asia/Shanghai",
        }
        if self.config.project_id:
            request_body["project_scope_selector"] = {
                "kind": "project",
                "id": self.config.project_id,
            }
        response = self._request(
            "POST",
            f"/v1/memory/episodes/{episode_id}/turns",
            json_body=request_body,
            idempotency_key=f"testagent-completed-turn-{attempt_id}",
        )
        if response.get("episode_id") != episode_id:
            raise MemoryClientError("Memory Service 返回了不匹配的 Episode。")
        if response.get("turn_id") != attempt_id:
            raise MemoryClientError("Memory Service 返回了不匹配的 Completed Turn。")
        sequence_no = response.get("sequence_no")
        if isinstance(sequence_no, bool) or not isinstance(sequence_no, int) or sequence_no < 1:
            raise MemoryClientError("Memory Service 返回了无效的轮次序号。")
        consolidation_status = response.get("consolidation_status")
        if consolidation_status not in {None, "pending", "leased", "completed", "dead_letter"}:
            raise MemoryClientError("Memory Service 返回了无效的 Consolidation 状态。")
        consolidation_id = response.get("consolidation_id")
        if consolidation_id is not None:
            try:
                UUID(consolidation_id)
            except (TypeError, ValueError) as exc:
                raise MemoryClientError(
                    "Memory Service 返回了无效的 Consolidation ID。"
                ) from exc
        extraction = response.get("per_turn_extraction")
        if not isinstance(extraction, dict):
            raise MemoryClientError("Completed Turn 缺少即时 extraction 结果。")
        candidates = extraction.get("candidates")
        if not isinstance(candidates, list):
            raise MemoryClientError("Completed Turn extraction 结构无效。")
        extraction_summary = self._projection_summary(candidates)
        if extraction.get("candidate_count") != extraction_summary["candidate_count"]:
            raise MemoryClientError("Completed Turn extraction 候选数量不一致。")
        response["per_turn_extraction"] = {**extraction, **extraction_summary}
        return response

    def _projection_summary(self, candidates: list[Any]) -> dict[str, int]:
        operation_ids: list[str] = []
        ready_count = 0
        for candidate in candidates:
            operation = candidate.get("operation") if isinstance(candidate, dict) else None
            if not isinstance(operation, dict):
                raise MemoryClientError("Memory extraction 缺少 operation。")
            operation_id = _required_uuid(operation, "operation_id")
            projection_status = operation.get("projection_status")
            if projection_status == "ready":
                ready_count += 1
            elif projection_status == "pending":
                operation_ids.append(operation_id)

        completed = 0
        if operation_ids and self.config.projection_timeout_seconds > 0:
            completed = self._wait_for_projections(operation_ids)
            ready_count += completed
        return {
            "candidate_count": len(candidates),
            "ready_count": min(ready_count, len(candidates)),
            "pending_count": len(operation_ids) - completed,
        }

    def _wait_for_projections(self, operation_ids: list[str]) -> int:
        deadline = time.monotonic() + self.config.projection_timeout_seconds
        pending = set(operation_ids)
        ready = 0
        delay = 0.1
        while pending and time.monotonic() < deadline:
            for operation_id in tuple(pending):
                operation = self._request("GET", f"/v1/memory/operations/{operation_id}")
                status = operation.get("projection_status")
                if status == "ready":
                    ready += 1
                    pending.remove(operation_id)
                elif status in {"failed", "deleted", "not_applicable"}:
                    pending.remove(operation_id)
            if pending:
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
        return ready

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token()}"}
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

    def _token(self) -> str:
        if self.config.bearer_token is not None:
            return self.config.bearer_token
        assert self.config.jwt_secret is not None
        now = int(self._clock())
        header = _base64_json({"alg": "HS256", "typ": "JWT"})
        claims = _base64_json(
            {
                "iss": self.config.jwt_issuer,
                "aud": self.config.jwt_audience,
                "sub": self.config.user_id,
                "tenant_id": self.config.tenant_id,
                "user_id": self.config.user_id,
                "tenant_scope": False,
                "project_ids": [self.config.project_id] if self.config.project_id else [],
                "agent_ids": [],
                "permissions": ["memory:*"],
                "iat": now,
                "nbf": now - 1,
                "exp": now + 15 * 60,
            }
        )
        signing_input = f"{header}.{claims}"
        signature = hmac.new(
            self.config.jwt_secret.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{signing_input}.{_base64url(signature)}"


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


def _load_memory_env_file(raw_path: str) -> dict[str, str]:
    if not raw_path:
        return {}
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise MemoryClientError("MEMORY_ENV_FILE 必须是绝对路径。")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MemoryClientError("无法读取 MEMORY_ENV_FILE。") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MemoryClientError("MEMORY_ENV_FILE 必须是普通文件且不能是符号链接。")
    if metadata.st_size > 64 * 1024:
        raise MemoryClientError("MEMORY_ENV_FILE 超过 64 KiB。")
    if metadata.st_mode & 0o077:
        raise MemoryClientError("MEMORY_ENV_FILE 权限必须限制为当前用户可读写。")
    values = dotenv_values(path)
    return {key: str(value) for key, value in values.items() if value is not None}


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


def _task_context(task_id: str, attempt_id: str) -> dict[str, str]:
    UUID(task_id)
    UUID(attempt_id)
    return {"task_id": task_id, "attempt_id": attempt_id, "purpose": "answer_user"}


def _required_uuid(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise MemoryClientError(f"Memory Service 响应缺少 {key}。")
    try:
        UUID(value)
    except ValueError as exc:
        raise MemoryClientError(f"Memory Service 返回了无效的 {key}。") from exc
    return value


def _validate_task_binding(payload: dict[str, Any], task_id: str, attempt_id: str) -> None:
    if (
        payload.get("task_id") != task_id
        or payload.get("attempt_id") != attempt_id
        or payload.get("purpose") != "answer_user"
    ):
        raise MemoryClientError("Memory Context Pack 的任务绑定不匹配。")


def _context_items(pack: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    sections = pack.get("sections")
    if not isinstance(sections, list):
        raise MemoryClientError("Memory Context Pack 缺少 sections。")
    items: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("items"), list):
            raise MemoryClientError("Memory Context Pack section 结构无效。")
        kind = section.get("kind")
        for item in section["items"]:
            if not isinstance(item, dict) or item.get("trust") != "untrusted_memory_data":
                raise MemoryClientError("Memory Context Pack 包含未标记为不可信的数据。")
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                raise MemoryClientError("Memory Context Pack item 缺少正文。")
            memory_record_id = _required_uuid(item, "memory_record_id")
            memory_version_id = _required_uuid(item, "memory_version_id")
            items.append(
                {
                    "kind": kind,
                    "text": text,
                    "memory_record_id": memory_record_id,
                    "memory_version_id": memory_version_id,
                    "source_refs": item.get("source_refs", []),
                    "trust": "untrusted_memory_data",
                }
            )
    return tuple(items)


def _base64_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _base64url(encoded)


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


__all__ = [
    "MemoryClientError",
    "MemoryConfig",
    "MemoryContext",
    "MemoryHttpClient",
    "build_memory_client_from_env",
    "memory_configuration_status",
]
