"""Web Capability Service 的同步薄客户端。

Agent 只知道独立服务地址，不接触搜索供应商密钥，也不负责网页抓取安全。
SSRF、逐跳重定向校验和正文提取都必须由服务端完成。
"""

from __future__ import annotations

import os
from uuid import uuid4
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:3020"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_QUERY_CHARS = 400
MAX_URL_CHARS = 2_048
MAX_SERVICE_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class WebCapabilityConfig:
    """Agent 调用独立 Web 服务所需的最小配置。"""

    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    agent_id: str | None = None
    tenant_id: str | None = None

    @classmethod
    def from_env(cls) -> "WebCapabilityConfig":
        """读取服务地址；搜索供应商 Key 有意不属于 Agent 配置。"""
        base_url = os.getenv("WEB_CAPABILITY_BASE_URL", DEFAULT_BASE_URL).strip()
        agent_id = os.getenv("WEB_CAPABILITY_AGENT_ID", "").strip() or None
        tenant_id = os.getenv("WEB_CAPABILITY_TENANT_ID", "").strip() or None
        return cls(base_url=base_url, agent_id=agent_id, tenant_id=tenant_id)

    def validated_base_url(self) -> str:
        """校验服务地址并去掉末尾斜杠，避免请求路径拼接歧义。"""
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("WEB_CAPABILITY_BASE_URL 必须是有效的 HTTP/HTTPS 地址。")
        if parsed.username or parsed.password:
            raise ValueError("WEB_CAPABILITY_BASE_URL 不允许包含用户名或密码。")
        if parsed.query or parsed.fragment:
            raise ValueError("WEB_CAPABILITY_BASE_URL 不允许包含 query 或 fragment。")
        if self.timeout_seconds <= 0:
            raise ValueError("Web Capability 超时必须大于 0 秒。")
        return self.base_url.rstrip("/")


class WebCapabilityClient:
    """把同步 Agent 工具调用映射为独立服务 HTTP 请求。"""

    def __init__(
        self,
        config: WebCapabilityConfig | None = None,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config or WebCapabilityConfig.from_env()
        self.base_url = self.config.validated_base_url()
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(self.config.timeout_seconds),
            headers={
                "Accept": "application/json",
                "User-Agent": "TestAgent-WebCapabilityClient/1.0",
            },
        )

    def search(self, query: str) -> dict[str, Any]:
        """搜索网页；MVP 固定最多返回 5 条，避免模型扩大成本。"""
        if not isinstance(query, str) or not query.strip():
            return _error("INVALID_ARGUMENTS", "query 必须是非空字符串。")
        query = query.strip()
        if len(query) > MAX_QUERY_CHARS:
            return _error(
                "INVALID_ARGUMENTS",
                f"query 长度超过 {MAX_QUERY_CHARS} 字符。",
            )
        return self._post("/v1/search", {"query": query, "max_results": 5})

    def fetch(self, url: str) -> dict[str, Any]:
        """请求服务端安全抓取一个静态公开网页。"""
        if not isinstance(url, str) or not url.strip():
            return _error("INVALID_ARGUMENTS", "url 必须是非空字符串。")
        url = url.strip()
        if len(url) > MAX_URL_CHARS:
            return _error(
                "INVALID_ARGUMENTS",
                f"URL 长度超过 {MAX_URL_CHARS} 字符。",
            )
        return self._post("/v1/fetch", {"url": url})

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """统一处理连接错误、超时、响应上限和结构化服务错误。"""
        headers = {"X-Request-ID": f"agent-{uuid4().hex}"}
        if self.config.agent_id:
            headers["X-Agent-ID"] = self.config.agent_id
        if self.config.tenant_id:
            headers["X-Tenant-ID"] = self.config.tenant_id
        try:
            response = self._client.post(
                f"{self.base_url}{path}",
                json=payload,
                headers=headers,
            )
        except httpx.TimeoutException:
            return _error(
                "WEB_CAPABILITY_TIMEOUT",
                "Web Capability Service 请求超时。",
                retryable=True,
            )
        except httpx.RequestError as exc:
            return _error(
                "WEB_CAPABILITY_UNAVAILABLE",
                f"Web Capability Service 不可用：{type(exc).__name__}",
                retryable=True,
            )

        if len(response.content) > MAX_SERVICE_RESPONSE_BYTES:
            return _error(
                "INVALID_SERVICE_RESPONSE",
                "Web Capability Service 响应超过大小限制。",
            )
        try:
            result = response.json()
        except ValueError:
            return _error(
                "INVALID_SERVICE_RESPONSE",
                f"Web Capability Service 返回了非 JSON 响应（HTTP {response.status_code}）。",
            )
        if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
            return _error(
                "INVALID_SERVICE_RESPONSE",
                "Web Capability Service 返回结构无效。",
            )

        # 服务端的结构化错误比通用 HTTP 错误更有助于 Agent 决定是否换来源或重试。
        if not response.is_success and result.get("success") is not False:
            return _error(
                "WEB_CAPABILITY_ERROR",
                f"Web Capability Service 返回 HTTP {response.status_code}。",
                retryable=response.status_code >= 500,
            )
        return result


@lru_cache(maxsize=1)
def default_web_capability_client() -> WebCapabilityClient:
    """进程内复用 HTTP 连接池；首次真实调用时才读取环境变量。"""
    return WebCapabilityClient()


def _error(
    error_type: str,
    message: str,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
    }
