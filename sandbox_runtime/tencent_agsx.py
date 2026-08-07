"""腾讯云 AGSX 的最小 Provider Adapter。"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sandbox_runtime.models import (
    CleanupResult,
    ExecutionResult,
    ProviderCapabilities,
    RequestContext,
    SandboxKind,
    SandboxSession,
    utc_now,
)


SandboxFactory = Callable[[str, int, dict[str, str]], Any]
SandboxLister = Callable[[dict[str, str]], Iterable[Any]]
SandboxKiller = Callable[[str], bool]


@dataclass(frozen=True)
class TencentAGSXConfig:
    api_key: str
    domain: str
    code_template: str
    browser_template: str | None = None

    @classmethod
    def from_env(
        cls,
        *,
        require_browser: bool = False,
        default_browser_template: str | None = None,
    ) -> "TencentAGSXConfig":
        values = {
            "E2B_API_KEY": os.getenv("E2B_API_KEY", "").strip(),
            "E2B_DOMAIN": os.getenv("E2B_DOMAIN", "").strip(),
            "AGSX_TEMPLATE": os.getenv("AGSX_TEMPLATE", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        if not values["E2B_API_KEY"].startswith("e2b_"):
            raise RuntimeError("E2B_API_KEY must use the e2b_ compatible format.")
        browser_template = (
            os.getenv("AGSX_BROWSER_TEMPLATE", "").strip()
            or default_browser_template
        )
        if require_browser and not browser_template:
            raise RuntimeError("Missing required environment variable: AGSX_BROWSER_TEMPLATE")
        return cls(
            api_key=values["E2B_API_KEY"],
            domain=values["E2B_DOMAIN"],
            code_template=values["AGSX_TEMPLATE"],
            browser_template=browser_template,
        )


class TencentAGSXProvider:
    """封装 AGSX Code/Browser、文件传输、清理与孤儿扫描。"""

    provider_name = "tencent-agsx"

    def __init__(
        self,
        config: TencentAGSXConfig,
        *,
        code_factory: SandboxFactory | None = None,
        browser_factory: SandboxFactory | None = None,
        lister: SandboxLister | None = None,
        killer: SandboxKiller | None = None,
    ) -> None:
        self.config = config
        self._code_factory = code_factory or self._create_code_sandbox
        self._browser_factory = browser_factory or self._create_browser_sandbox
        self._lister = lister or self._list_managed_sandboxes
        self._killer = killer or self._kill_by_id

    def capabilities(self) -> ProviderCapabilities:
        kinds = [SandboxKind.CODE]
        if self.config.browser_template:
            kinds.append(SandboxKind.BROWSER)
        return ProviderCapabilities(
            provider=self.provider_name,
            kinds=tuple(kinds),
            languages=("python", "javascript"),
            network_modes=("PUBLIC", "SANDBOX", "VPC"),
            supports_file_transfer=True,
            supports_pause=True,
            supports_domain_egress_policy=False,
            max_ttl_seconds=24 * 60 * 60,
        )

    def create_code(
        self,
        context: RequestContext,
        ttl_seconds: int = 300,
    ) -> SandboxSession:
        return self._create_session(
            context,
            SandboxKind.CODE,
            self.config.code_template,
            ttl_seconds,
            self._code_factory,
        )

    def create_browser(
        self,
        context: RequestContext,
        ttl_seconds: int = 300,
    ) -> SandboxSession:
        if not self.config.browser_template:
            raise RuntimeError("Browser capability is not configured.")
        return self._create_session(
            context,
            SandboxKind.BROWSER,
            self.config.browser_template,
            ttl_seconds,
            self._browser_factory,
        )

    def execute_code(
        self,
        session: SandboxSession,
        code: str,
        *,
        language: str = "python",
        timeout_seconds: float = 30,
    ) -> ExecutionResult:
        if session.kind is not SandboxKind.CODE:
            return ExecutionResult(
                success=False,
                error_type="INVALID_SPEC",
                error_message="execute_code requires a code sandbox session.",
            )
        started = time.perf_counter()
        try:
            execution = session.native.run_code(
                code,
                language=language,
                timeout=timeout_seconds,
            )
            stdout = "".join(str(item) for item in execution.logs.stdout)
            stderr = "".join(str(item) for item in execution.logs.stderr)
            if execution.error is not None:
                return ExecutionResult(
                    success=False,
                    stdout=stdout,
                    stderr=stderr,
                    error_type="EXECUTION_FAILED",
                    error_message=str(execution.error),
                    execution_count=getattr(execution, "execution_count", None),
                    duration_ms=_duration_ms(started),
                )
            return ExecutionResult(
                success=True,
                stdout=stdout,
                stderr=stderr,
                execution_count=getattr(execution, "execution_count", None),
                duration_ms=_duration_ms(started),
            )
        except TimeoutError as exc:
            return ExecutionResult(
                success=False,
                error_type="EXECUTION_TIMEOUT",
                error_message=self._safe_message(exc),
                duration_ms=_duration_ms(started),
            )
        except Exception as exc:
            message = self._safe_message(exc)
            error_type = (
                "EXECUTION_TIMEOUT"
                if "timeout" in message.lower() or "timed out" in message.lower()
                else "PROVIDER_UNAVAILABLE"
            )
            return ExecutionResult(
                success=False,
                error_type=error_type,
                error_message=message,
                duration_ms=_duration_ms(started),
            )

    def run_command(
        self,
        session: SandboxSession,
        command: str,
        *,
        timeout_seconds: float = 30,
    ) -> ExecutionResult:
        started = time.perf_counter()
        try:
            result = session.native.commands.run(command, timeout=timeout_seconds)
            return ExecutionResult(
                success=result.exit_code == 0,
                stdout=result.stdout,
                stderr=result.stderr,
                error_type=None if result.exit_code == 0 else "EXECUTION_FAILED",
                error_message=None if result.exit_code == 0 else f"exit code {result.exit_code}",
                duration_ms=_duration_ms(started),
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                error_type="PROVIDER_UNAVAILABLE",
                error_message=self._safe_message(exc),
                duration_ms=_duration_ms(started),
            )

    def put_file(self, session: SandboxSession, remote_path: str, data: bytes) -> None:
        session.native.files.write(remote_path, data)

    def get_file(self, session: SandboxSession, remote_path: str) -> bytes:
        return bytes(session.native.files.read(remote_path, format="bytes"))

    def browser_urls(self, session: SandboxSession) -> tuple[str, str, dict[str, str]]:
        """返回 CDP/live URL；调用方不得记录包含短期 token 的完整 URL。"""
        if session.kind is not SandboxKind.BROWSER:
            raise ValueError("browser_urls requires a browser session.")
        token = getattr(session.native, "_envd_access_token", "")
        if not token:
            raise RuntimeError("Browser sandbox did not expose an access token.")
        host = session.native.get_host(9000)
        cdp_url = f"https://{host}/cdp?access_token={token}"
        live_url = (
            f"https://{host}/novnc/vnc_lite.html?access_token={token}"
            f"&path=websockify%3Faccess_token%3D{token}"
        )
        return cdp_url, live_url, {"X-Access-Token": str(token)}

    def terminate(self, session: SandboxSession) -> CleanupResult:
        try:
            destroyed = bool(session.native.kill())
            return CleanupResult(session.sandbox_id, destroyed)
        except Exception as exc:
            return CleanupResult(
                session.sandbox_id,
                False,
                self._safe_message(exc),
            )

    def cleanup_expired(self, max_age_seconds: int) -> list[CleanupResult]:
        """只清理带 managed_by=lazyx-testagent metadata 的过期实例。"""
        cutoff = utc_now() - timedelta(seconds=max_age_seconds)
        results: list[CleanupResult] = []
        for info in self._lister({"managed_by": "lazyx-testagent"}):
            if info.started_at > cutoff:
                continue
            sandbox_id = str(info.sandbox_id)
            try:
                destroyed = bool(self._killer(sandbox_id))
                results.append(CleanupResult(sandbox_id, destroyed))
            except Exception as exc:
                results.append(CleanupResult(sandbox_id, False, self._safe_message(exc)))
        return results

    def _create_session(
        self,
        context: RequestContext,
        kind: SandboxKind,
        template: str,
        ttl_seconds: int,
        factory: SandboxFactory,
    ) -> SandboxSession:
        if ttl_seconds < 1 or ttl_seconds > 24 * 60 * 60:
            raise ValueError("ttl_seconds must be between 1 and 86400.")
        metadata = context.metadata() | {"sandbox_kind": kind.value}
        native = factory(template, ttl_seconds, metadata)
        created_at = utc_now()
        return SandboxSession(
            sandbox_id=str(native.sandbox_id),
            provider=self.provider_name,
            kind=kind,
            template=template,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=ttl_seconds),
            metadata=metadata,
            native=native,
        )

    def _create_code_sandbox(
        self,
        template: str,
        timeout: int,
        metadata: dict[str, str],
    ) -> Any:
        from e2b_code_interpreter import Sandbox

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
        from e2b import Sandbox

        return Sandbox.create(
            template=template,
            timeout=timeout,
            metadata=metadata,
            api_key=self.config.api_key,
            domain=self.config.domain,
        )

    def _list_managed_sandboxes(self, metadata: dict[str, str]) -> list[Any]:
        from e2b.sandbox.sandbox_api import SandboxQuery
        from e2b_code_interpreter import Sandbox

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
        from e2b_code_interpreter import Sandbox

        return bool(
            Sandbox.kill(
                sandbox_id,
                api_key=self.config.api_key,
                domain=self.config.domain,
            )
        )

    def _safe_message(self, exc: Exception) -> str:
        return str(exc).replace(self.config.api_key, "[REDACTED]")


def _duration_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
