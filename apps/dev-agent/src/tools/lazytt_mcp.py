"""把一个远程 LazyTT MCP Server 适配成 AgentLoop 的普通工具。"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import httpx2
from dotenv import dotenv_values
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from testagent_dev_agent.paths import REPOSITORY_ROOT


DEFAULT_MCP_ENV_FILE = REPOSITORY_ROOT / ".local" / "lazytt-mcp.env"
DEFAULT_TIMEOUT_SECONDS = 30.0
TOKEN_REFRESH_MARGIN_SECONDS = 30.0
MAX_RESULT_TEXT_CHARS = 4_000
SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


class LazyTtMcpError(RuntimeError):
    """不携带 Token、Secret 或远程响应正文的 MCP 接入错误。"""


@dataclass(frozen=True)
class LazyTtMcpConfig:
    server_name: str
    server_url: str
    token_url: str
    client_id: str
    client_secret: str = field(repr=False)
    tool_allowlist: tuple[str, ...] = ("area_calculate",)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> LazyTtMcpConfig | None:
        values = _mcp_env_values()
        if not _boolean(values.get("MCP_ENABLED", "false"), "MCP_ENABLED"):
            return None

        server_name = values.get("MCP_SERVER_NAME", "lazytt").strip()
        server_url = values.get("MCP_SERVER_URL", "").strip()
        token_url = values.get("OIDC_TOKEN_URL", "").strip()
        client_id = values.get("OIDC_CLIENT_ID", "").strip()
        client_secret = values.get("OIDC_CLIENT_SECRET", "").strip()
        allowlist = tuple(
            dict.fromkeys(
                name.strip()
                for name in values.get("MCP_TOOL_ALLOWLIST", "area_calculate").split(",")
                if name.strip()
            )
        )

        if not SERVER_NAME_PATTERN.fullmatch(server_name):
            raise LazyTtMcpError(
                "MCP_SERVER_NAME 只能包含字母、数字、下划线或连字符，且最多 24 个字符。"
            )
        _validate_url("MCP_SERVER_URL", server_url)
        _validate_url("OIDC_TOKEN_URL", token_url)
        if not client_id:
            raise LazyTtMcpError("缺少 OIDC_CLIENT_ID。")
        if not client_secret:
            raise LazyTtMcpError("缺少 OIDC_CLIENT_SECRET。")
        if not allowlist:
            raise LazyTtMcpError("MCP_TOOL_ALLOWLIST 至少需要一个工具名。")

        timeout_raw = values.get(
            "MCP_CALL_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)
        ).strip()
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError as exc:
            raise LazyTtMcpError("MCP_CALL_TIMEOUT_SECONDS 必须是数字。") from exc
        if not 1 <= timeout_seconds <= 120:
            raise LazyTtMcpError("MCP_CALL_TIMEOUT_SECONDS 必须在 1 到 120 之间。")

        return cls(
            server_name=server_name,
            server_url=server_url,
            token_url=token_url,
            client_id=client_id,
            client_secret=client_secret,
            tool_allowlist=allowlist,
            timeout_seconds=timeout_seconds,
        )


class LazyTtMcpClient:
    """同步外观的最小 MCP Client；每次调用建立一个短生命周期 Session。"""

    def __init__(self, config: LazyTtMcpConfig) -> None:
        self.config = config
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    def list_tools(self) -> list[Any]:
        try:
            access_token = self._access_token()
            return asyncio.run(self._list_tools(access_token))
        except Exception as exc:
            raise LazyTtMcpError("无法读取 LazyTT MCP 工具目录。") from exc

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            access_token = self._access_token()
            result = asyncio.run(self._call_tool(access_token, name, arguments))
        except Exception:
            return {
                "success": False,
                "source": "mcp",
                "server": self.config.server_name,
                "tool": name,
                "error_type": "MCP_TRANSPORT_ERROR",
                "message": "远程 MCP 工具暂时不可用。",
                "retryable": True,
            }
        return _normalize_tool_result(self.config.server_name, name, result)

    def _access_token(self) -> str:
        now = time.monotonic()
        with self._token_lock:
            if (
                self._token is not None
                and now + TOKEN_REFRESH_MARGIN_SECONDS < self._token_expires_at
            ):
                return self._token

            try:
                with httpx.Client(
                    timeout=self.config.timeout_seconds,
                    trust_env=False,
                ) as client:
                    response = client.post(
                        self.config.token_url,
                        data={
                            "grant_type": "client_credentials",
                            "client_id": self.config.client_id,
                            "client_secret": self.config.client_secret,
                        },
                        headers={"Accept": "application/json"},
                    )
            except httpx.HTTPError as exc:
                raise LazyTtMcpError("无法连接 MCP 身份服务。") from exc
            if response.status_code != 200:
                raise LazyTtMcpError(
                    f"MCP 身份服务拒绝了 Token 请求（HTTP {response.status_code}）。"
                )
            try:
                payload = response.json()
                token = payload["access_token"]
                expires_in = float(payload.get("expires_in", 300))
            except (KeyError, TypeError, ValueError) as exc:
                raise LazyTtMcpError("MCP 身份服务返回了无效的 Token 响应。") from exc
            if not isinstance(token, str) or not token.strip() or expires_in <= 0:
                raise LazyTtMcpError("MCP 身份服务返回了无效的 Token 响应。")
            self._token = token.strip()
            self._token_expires_at = time.monotonic() + expires_in
            return self._token

    async def _list_tools(self, access_token: str) -> list[Any]:
        async with self._connect(access_token) as client:
            result = await client.list_tools(cache_mode="refresh")
            return list(result.tools)

    async def _call_tool(
        self,
        access_token: str,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        async with self._connect(access_token) as client:
            return await client.call_tool(name, arguments)

    @asynccontextmanager
    async def _connect(self, access_token: str) -> AsyncIterator[Client]:
        timeout = httpx2.Timeout(
            self.config.timeout_seconds,
            read=self.config.timeout_seconds,
        )
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        ) as http_client:
            transport = streamable_http_client(
                self.config.server_url,
                http_client=http_client,
            )
            async with Client(
                transport,
                read_timeout_seconds=self.config.timeout_seconds,
                mode="auto",
            ) as client:
                yield client


@dataclass(frozen=True)
class LazyTtMcpTools:
    tool_definitions: dict[str, dict[str, Any]]
    tool_handlers: dict[str, Callable[..., dict[str, Any]]]
    tool_budgets: dict[str, int]

    @classmethod
    def from_config(
        cls,
        config: LazyTtMcpConfig,
        *,
        client: LazyTtMcpClient | None = None,
    ) -> LazyTtMcpTools:
        resolved_client = client or LazyTtMcpClient(config)
        remote_tools = {tool.name: tool for tool in resolved_client.list_tools()}
        missing = [name for name in config.tool_allowlist if name not in remote_tools]
        if missing:
            raise LazyTtMcpError(
                "LazyTT MCP 未发布配置的工具：" + ", ".join(missing)
            )

        definitions: dict[str, dict[str, Any]] = {}
        handlers: dict[str, Callable[..., dict[str, Any]]] = {}
        budgets: dict[str, int] = {}
        for remote_name in config.tool_allowlist:
            remote_tool = remote_tools[remote_name]
            local_name = _local_tool_name(config.server_name, remote_name)
            input_schema = _normalize_model_tool_schema(remote_tool.input_schema)
            if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
                raise LazyTtMcpError(
                    f"LazyTT MCP 工具 {remote_name} 的 inputSchema 不是 JSON 对象。"
                )
            description = str(remote_tool.description or "远程企业能力。").strip()
            definitions[local_name] = {
                "type": "function",
                "function": {
                    "name": local_name,
                    "description": f"[LazyTT MCP] {description}",
                    "parameters": input_schema,
                },
            }
            handlers[local_name] = _tool_handler(resolved_client, remote_name)
            budgets[local_name] = 1
        return cls(definitions, handlers, budgets)


def build_lazytt_mcp_tools_from_env() -> LazyTtMcpTools | None:
    config = LazyTtMcpConfig.from_env()
    return None if config is None else LazyTtMcpTools.from_config(config)


def _tool_handler(
    client: LazyTtMcpClient,
    remote_name: str,
) -> Callable[..., dict[str, Any]]:
    def call(**arguments: Any) -> dict[str, Any]:
        return client.call_tool(remote_name, arguments)

    return call


def _normalize_model_tool_schema(value: Any) -> Any:
    """补齐模型 Function Tool 方言要求、但 MCP Schema 可能省略的类型。"""
    if isinstance(value, list):
        return [_normalize_model_tool_schema(item) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    normalized = {
        key: _normalize_model_tool_schema(item) for key, item in value.items()
    }
    enum = normalized.get("enum")
    if "type" not in normalized and isinstance(enum, list) and enum:
        inferred = {_json_schema_type(item) for item in enum}
        if len(inferred) == 1 and None not in inferred:
            normalized["type"] = inferred.pop()
    return normalized


def _json_schema_type(value: Any) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    return None


def _normalize_tool_result(server_name: str, tool_name: str, result: Any) -> dict[str, Any]:
    if bool(getattr(result, "is_error", False)):
        return {
            "success": False,
            "source": "mcp",
            "server": server_name,
            "tool": tool_name,
            "error_type": "MCP_TOOL_ERROR",
            "message": "远程 MCP 工具返回了业务错误。",
            "retryable": False,
        }

    structured = getattr(result, "structured_content", None)
    if structured is None:
        text_parts = [
            item.text
            for item in getattr(result, "content", ())
            if isinstance(getattr(item, "text", None), str)
        ]
        structured = {"text": "\n".join(text_parts)[:MAX_RESULT_TEXT_CHARS]}
    return {
        "success": True,
        "source": "mcp",
        "server": server_name,
        "tool": tool_name,
        "result": structured,
    }


def _mcp_env_values() -> dict[str, str]:
    configured_path = os.getenv("MCP_ENV_FILE", "").strip()
    env_path = Path(configured_path).expanduser() if configured_path else DEFAULT_MCP_ENV_FILE
    if not env_path.is_absolute():
        env_path = REPOSITORY_ROOT / env_path
    file_values: Mapping[str, Any] = dotenv_values(env_path) if env_path.is_file() else {}

    names = (
        "MCP_ENABLED",
        "MCP_SERVER_NAME",
        "MCP_SERVER_URL",
        "OIDC_TOKEN_URL",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "MCP_TOOL_ALLOWLIST",
        "MCP_CALL_TIMEOUT_SECONDS",
    )
    return {
        name: str(
            os.getenv(name)
            if os.getenv(name) is not None
            else file_values.get(name, "") or ""
        )
        for name in names
    }


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise LazyTtMcpError(f"{name} 必须是 true 或 false。")


def _validate_url(name: str, value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LazyTtMcpError(f"{name} 必须是无凭据、Query 和 Fragment 的 HTTP(S) URL。")


def _local_tool_name(server_name: str, remote_name: str) -> str:
    local_name = f"mcp__{server_name}__{remote_name}"
    if len(local_name) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", local_name):
        raise LazyTtMcpError(f"MCP 工具名无法安全映射：{remote_name}")
    return local_name


__all__ = [
    "LazyTtMcpClient",
    "LazyTtMcpConfig",
    "LazyTtMcpError",
    "LazyTtMcpTools",
    "build_lazytt_mcp_tools_from_env",
]
