"""PPIO Agent Sandbox 的 Provider 适配器。

业务层继续复用统一 Provider 协议，但云端调用使用 PPIO 官方 Python SDK。
不能直接复用项目中的新版 E2B SDK：它会在客户端把 PPIO 的 ``sk_`` 密钥
误判为非法的 E2B 密钥格式。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sandbox_runtime.models import SandboxKind, SandboxSession
from sandbox_runtime.tencent_agsx import TencentAGSXProvider


@dataclass(frozen=True)
class PPIOSandboxConfig:
    api_key: str
    domain: str = "sandbox.ppio.cn"
    code_template: str = ""
    browser_template: str | None = None
    browser_cdp_port: int = 9223

    @classmethod
    def from_env(cls, *, require_browser: bool = False) -> "PPIOSandboxConfig":
        explicit_domain = os.getenv("PPIO_DOMAIN", "").strip()
        if not explicit_domain and os.getenv("E2B_DOMAIN", "").strip() == "sandbox.ppio.cn":
            explicit_domain = "sandbox.ppio.cn"
        # 不得把腾讯 AGSX 的 E2B_API_KEY 静默发到 PPIO。只有调用方明确设置
        # PPIO_DOMAIN，或把 E2B_DOMAIN 明确设为 sandbox.ppio.cn 时，才允许
        # 使用官方 E2B 兼容文档中的 E2B_API_KEY 命名。
        api_key = os.getenv("PPIO_API_KEY", "").strip()
        if not api_key and explicit_domain:
            api_key = os.getenv("E2B_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "Missing PPIO_API_KEY (or set PPIO_DOMAIN explicitly before reusing E2B_API_KEY)."
            )
        domain = explicit_domain or "sandbox.ppio.cn"
        code_template = os.getenv("PPIO_TEMPLATE", "").strip()
        browser_template = os.getenv("PPIO_BROWSER_TEMPLATE", "").strip() or None
        raw_browser_port = os.getenv("PPIO_BROWSER_CDP_PORT", "9223").strip()
        try:
            browser_cdp_port = int(raw_browser_port)
        except ValueError as exc:
            raise RuntimeError("PPIO_BROWSER_CDP_PORT must be an integer.") from exc
        if browser_cdp_port < 1 or browser_cdp_port > 65535:
            raise RuntimeError("PPIO_BROWSER_CDP_PORT must be between 1 and 65535.")
        if require_browser and not browser_template:
            raise RuntimeError(
                "PPIO_BROWSER_TEMPLATE is not configured; PPIO Browser/Desktop is not "
                "automatically equivalent to the AGSX Browser Tool."
            )
        return cls(
            api_key=api_key,
            domain=domain,
            code_template=code_template,
            browser_template=browser_template,
            browser_cdp_port=browser_cdp_port,
        )


class PPIOSandboxProvider(TencentAGSXProvider):
    """通过 PPIO 官方 SDK 调用 Code 与 browser-chromium 沙箱。"""

    provider_name = "ppio-sandbox"

    def __init__(self, config: PPIOSandboxConfig, **kwargs: object) -> None:
        super().__init__(config, **kwargs)  # type: ignore[arg-type]

    def _create_code_sandbox(
        self,
        template: str,
        timeout: int,
        metadata: dict[str, str],
    ) -> Any:
        from ppio_sandbox.code_interpreter import Sandbox

        options: dict[str, Any] = {
            "timeout": timeout,
            "metadata": metadata,
            "api_key": self.config.api_key,
            "domain": self.config.domain,
        }
        if template:
            options["template"] = template
        return Sandbox.create(**options)

    def _create_browser_sandbox(
        self,
        template: str,
        timeout: int,
        metadata: dict[str, str],
    ) -> Any:
        from ppio_sandbox.code_interpreter import Sandbox

        # PPIO 官方 browser-chromium 模板在 9223 暴露 CDP。secure=True 让
        # 公网入口必须携带实例级 token，避免生成裸露的调试端口。
        return Sandbox.create(
            template=template,
            timeout=timeout,
            metadata=metadata,
            secure=True,
            api_key=self.config.api_key,
            domain=self.config.domain,
        )

    def browser_urls(self, session: SandboxSession) -> tuple[str, str, dict[str, str]]:
        if session.kind is not SandboxKind.BROWSER:
            raise ValueError("browser_urls requires a browser session.")
        token = getattr(session.native, "_envd_access_token", "")
        if not token:
            raise RuntimeError("PPIO Browser sandbox did not expose an access token.")
        port = self.config.browser_cdp_port
        host = session.native.get_host(port)
        cdp_url = f"https://{host}"
        return cdp_url, "", {"X-Access-Token": str(token)}

    def _list_managed_sandboxes(self, metadata: dict[str, str]) -> list[Any]:
        from ppio_sandbox.code_interpreter import Sandbox
        from ppio_sandbox.core.sandbox.sandbox_api import SandboxQuery

        paginator = Sandbox.list(
            query=SandboxQuery(metadata=metadata),
            api_key=self.config.api_key,
            domain=self.config.domain,
        )
        items: list[Any] = []
        while paginator.has_next:
            items.extend(paginator.next_items())
        return items

    def _kill_by_id(self, sandbox_id: str) -> bool:
        from ppio_sandbox.code_interpreter import Sandbox

        return bool(
            Sandbox.kill(
                sandbox_id,
                api_key=self.config.api_key,
                domain=self.config.domain,
            )
        )
