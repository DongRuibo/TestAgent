"""Gateway V0 的 Agent API Key 鉴权。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class GatewayIdentity:
    tenant_id: str
    agent_id: str
    allowed_tasks: frozenset[str] = frozenset(
        {"code", "sales_analysis", "spreadsheet_analysis", "browser_public"}
    )


class GatewayAuth:
    """内存 API Key 映射；生产环境应替换为加密存储或 OIDC/JWT。"""

    def __init__(self, credentials: Mapping[str, GatewayIdentity]) -> None:
        self._credentials = dict(credentials)

    def authenticate(self, token: str | None) -> GatewayIdentity:
        if not token or token not in self._credentials:
            raise PermissionError("Invalid or missing Agent API key.")
        return self._credentials[token]
