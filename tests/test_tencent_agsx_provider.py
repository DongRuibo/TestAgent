"""TencentAGSXProvider 的离线生命周期和孤儿清理测试。"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from sandbox_runtime.models import RequestContext, SandboxKind, utc_now
from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider


class FakeFiles:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def write(self, path: str, data: bytes) -> None:
        self.data[path] = bytes(data)

    def read(self, path: str, format: str = "text"):
        assert format == "bytes"
        return bytearray(self.data[path])


class FakeSandbox:
    def __init__(self, sandbox_id: str = "sbi-test") -> None:
        self.sandbox_id = sandbox_id
        self.files = FakeFiles()
        self.killed = False
        self.calls: list[tuple[str, str | None, float]] = []
        self._envd_access_token = "temporary-browser-token"

    def run_code(self, code: str, language: str | None, timeout: float):
        self.calls.append((code, language, timeout))
        return SimpleNamespace(
            logs=SimpleNamespace(stdout=["ok\n"], stderr=[]),
            error=None,
            execution_count=1,
        )

    def kill(self) -> bool:
        self.killed = True
        return True

    def get_host(self, port: int) -> str:
        assert port == 9000
        return "browser.sandbox.example.com"


CONFIG = TencentAGSXConfig(
    api_key="e2b_test",
    domain="sandbox.example.com",
    code_template="code-tool",
    browser_template="browser-tool",
)
CONTEXT = RequestContext("tenant-1", "user-1", "task-1", "attempt-1")


def test_provider_creates_executes_transfers_and_terminates() -> None:
    sandbox = FakeSandbox()
    calls: list[tuple[str, int, dict[str, str]]] = []

    def factory(template: str, ttl: int, metadata: dict[str, str]) -> FakeSandbox:
        calls.append((template, ttl, metadata))
        return sandbox

    provider = TencentAGSXProvider(CONFIG, code_factory=factory)
    session = provider.create_code(CONTEXT, ttl_seconds=120)
    execution = provider.execute_code(
        session,
        "print('ok')",
        language="python",
        timeout_seconds=7,
    )
    provider.put_file(session, "/tmp/in.bin", b"payload")
    downloaded = provider.get_file(session, "/tmp/in.bin")
    cleanup = provider.terminate(session)

    assert session.kind is SandboxKind.CODE
    assert session.sandbox_id == "sbi-test"
    assert calls[0][0:2] == ("code-tool", 120)
    assert calls[0][2]["managed_by"] == "lazyx-testagent"
    assert calls[0][2]["attempt_id"] == "attempt-1"
    assert execution.success is True
    assert execution.stdout == "ok\n"
    assert sandbox.calls == [("print('ok')", "python", 7)]
    assert downloaded == b"payload"
    assert cleanup.destroyed is True
    assert sandbox.killed is True


def test_provider_capabilities_fail_closed_for_domain_policy() -> None:
    capabilities = TencentAGSXProvider(CONFIG).capabilities()

    assert SandboxKind.CODE in capabilities.kinds
    assert SandboxKind.BROWSER in capabilities.kinds
    assert capabilities.supports_domain_egress_policy is False


def test_provider_creates_browser_and_builds_cdp_urls() -> None:
    sandbox = FakeSandbox("browser-test")
    calls: list[tuple[str, int, dict[str, str]]] = []

    def factory(template: str, ttl: int, metadata: dict[str, str]) -> FakeSandbox:
        calls.append((template, ttl, metadata))
        return sandbox

    provider = TencentAGSXProvider(CONFIG, browser_factory=factory)
    session = provider.create_browser(CONTEXT, ttl_seconds=300)
    cdp_url, live_url, headers = provider.browser_urls(session)

    assert session.kind is SandboxKind.BROWSER
    assert calls[0][0:2] == ("browser-tool", 300)
    assert calls[0][2]["sandbox_kind"] == "browser"
    assert cdp_url.startswith("https://browser.sandbox.example.com/cdp?")
    assert "temporary-browser-token" in cdp_url
    assert "temporary-browser-token" in live_url
    assert headers == {"X-Access-Token": "temporary-browser-token"}


def test_cleanup_expired_only_kills_old_managed_items() -> None:
    old = SimpleNamespace(sandbox_id="old", started_at=utc_now() - timedelta(hours=1))
    fresh = SimpleNamespace(sandbox_id="fresh", started_at=utc_now())
    killed: list[str] = []
    provider = TencentAGSXProvider(
        CONFIG,
        lister=lambda metadata: [old, fresh]
        if metadata == {"managed_by": "lazyx-testagent"}
        else [],
        killer=lambda sandbox_id: killed.append(sandbox_id) or True,
    )

    results = provider.cleanup_expired(max_age_seconds=600)

    assert [item.sandbox_id for item in results] == ["old"]
    assert results[0].destroyed is True
    assert killed == ["old"]
