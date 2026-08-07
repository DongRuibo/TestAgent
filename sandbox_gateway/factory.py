"""从环境变量构建真实 Gateway；测试可直接注入 Fake Provider。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from sandbox_gateway.api import create_app
from sandbox_gateway.auth import GatewayAuth, GatewayIdentity
from sandbox_gateway.service import GatewayService
from sandbox_runtime.ppio import PPIOSandboxConfig, PPIOSandboxProvider
from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_service() -> GatewayService:
    load_dotenv(PROJECT_ROOT / ".env")
    providers = {}
    try:
        providers["tencent-agsx"] = TencentAGSXProvider(TencentAGSXConfig.from_env())
    except RuntimeError:
        pass
    try:
        providers["ppio-sandbox"] = PPIOSandboxProvider(PPIOSandboxConfig.from_env())
    except RuntimeError:
        pass
    if not providers:
        raise RuntimeError("No configured sandbox Provider. Check .env.")

    database_path = Path(
        os.getenv("GATEWAY_DATABASE_PATH", str(PROJECT_ROOT / "artifacts/gateway/gateway.sqlite3"))
    )
    artifact_root = Path(
        os.getenv("GATEWAY_ARTIFACT_ROOT", str(PROJECT_ROOT / "artifacts/gateway"))
    )
    return GatewayService(
        providers,
        database_path=database_path,
        artifact_root=artifact_root,
    )


def build_app():
    service = build_service()
    return create_app(service, build_auth())


def build_auth() -> GatewayAuth:
    raw = os.getenv("GATEWAY_API_KEYS", "demo-token|demo-tenant|demo-agent")
    credentials = {}
    for item in raw.split(","):
        token, tenant_id, agent_id = item.strip().split("|", 2)
        credentials[token] = GatewayIdentity(tenant_id=tenant_id, agent_id=agent_id)
    return GatewayAuth(credentials)


app = build_app()
