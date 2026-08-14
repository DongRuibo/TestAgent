"""Agent 原子工具：通过独立 Web Capability Service 搜索网页。"""

from __future__ import annotations

from typing import Any

from testagent_web_tools.client import (
    WebCapabilityClient,
    default_web_capability_client,
)


def web_search(
    query: str,
    *,
    client: WebCapabilityClient | None = None,
) -> dict[str, Any]:
    """返回统一搜索结果；标题和摘要仍然是不可信外部数据。"""
    capability = client or default_web_capability_client()
    return capability.search(query)
