"""Agent 原子工具：通过独立 Web Capability Service 读取静态网页。"""

from __future__ import annotations

from typing import Any

from tools.web_capability_client import (
    WebCapabilityClient,
    default_web_capability_client,
)


def web_fetch(
    url: str,
    *,
    client: WebCapabilityClient | None = None,
) -> dict[str, Any]:
    """读取公开静态网页；SSRF 与重定向安全边界在服务端落实。"""
    capability = client or default_web_capability_client()
    return capability.fetch(url)
