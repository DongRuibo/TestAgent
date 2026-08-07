"""PPIO E2B 兼容适配器的离线测试。"""

from __future__ import annotations

from sandbox_runtime.models import RequestContext, SandboxKind
from sandbox_runtime.ppio import PPIOSandboxConfig, PPIOSandboxProvider


class FakeSandbox:
    sandbox_id = "ppio-test"


def test_ppio_config_reads_explicit_environment(monkeypatch) -> None:
    monkeypatch.setenv("PPIO_API_KEY", "ppio_test_key")
    monkeypatch.setenv("PPIO_DOMAIN", "sandbox.example")
    monkeypatch.setenv("PPIO_TEMPLATE", "python-template")

    config = PPIOSandboxConfig.from_env()

    assert config.api_key == "ppio_test_key"
    assert config.domain == "sandbox.example"
    assert config.code_template == "python-template"
    assert config.browser_cdp_port == 9223


def test_ppio_config_reads_browser_cdp_port(monkeypatch) -> None:
    monkeypatch.setenv("PPIO_API_KEY", "ppio_test_key")
    monkeypatch.setenv("PPIO_BROWSER_CDP_PORT", "9444")

    config = PPIOSandboxConfig.from_env()

    assert config.browser_cdp_port == 9444


def test_ppio_config_rejects_invalid_browser_cdp_port(monkeypatch) -> None:
    monkeypatch.setenv("PPIO_API_KEY", "ppio_test_key")
    monkeypatch.setenv("PPIO_BROWSER_CDP_PORT", "invalid")

    try:
        PPIOSandboxConfig.from_env()
    except RuntimeError as exc:
        assert "PPIO_BROWSER_CDP_PORT" in str(exc)
    else:
        raise AssertionError("Invalid CDP port must fail closed")


def test_ppio_config_does_not_reuse_tencent_key_by_default(monkeypatch) -> None:
    monkeypatch.delenv("PPIO_API_KEY", raising=False)
    monkeypatch.delenv("PPIO_DOMAIN", raising=False)
    monkeypatch.setenv("E2B_API_KEY", "e2b_tencent_key")

    try:
        PPIOSandboxConfig.from_env()
    except RuntimeError as exc:
        assert "PPIO_API_KEY" in str(exc)
    else:
        raise AssertionError("Tencent E2B key must not silently select PPIO")


def test_ppio_config_accepts_official_e2b_compatibility_environment(monkeypatch) -> None:
    monkeypatch.delenv("PPIO_API_KEY", raising=False)
    monkeypatch.delenv("PPIO_DOMAIN", raising=False)
    monkeypatch.setenv("E2B_API_KEY", "e2b_ppio_key")
    monkeypatch.setenv("E2B_DOMAIN", "sandbox.ppio.cn")

    config = PPIOSandboxConfig.from_env()

    assert config.api_key == "e2b_ppio_key"
    assert config.domain == "sandbox.ppio.cn"


def test_ppio_provider_keeps_internal_provider_boundary() -> None:
    calls = []

    def factory(template, ttl, metadata):
        calls.append((template, ttl, metadata))
        return FakeSandbox()

    provider = PPIOSandboxProvider(
        PPIOSandboxConfig(api_key="ppio_test_key"),
        code_factory=factory,
    )
    session = provider.create_code(
        RequestContext("tenant", "user", "task", "attempt"),
    )

    assert session.provider == "ppio-sandbox"
    assert provider.capabilities().provider == "ppio-sandbox"
    assert SandboxKind.CODE in provider.capabilities().kinds
    assert calls[0][0] == ""


def test_ppio_browser_urls_use_configured_cdp_port_and_access_token() -> None:
    class FakeBrowserSandbox:
        sandbox_id = "ppio-browser"
        _envd_access_token = "temporary-ppio-token"

        @staticmethod
        def get_host(port: int) -> str:
            return f"{port}-browser.sandbox.ppio.cn"

    provider = PPIOSandboxProvider(
        PPIOSandboxConfig(
            api_key="ppio_test_key",
            browser_template="browser-chromium",
            browser_cdp_port=9444,
        ),
        browser_factory=lambda _template, _ttl, _metadata: FakeBrowserSandbox(),
    )
    session = provider.create_browser(
        RequestContext("tenant", "user", "task", "attempt"),
    )

    cdp_url, live_url, headers = provider.browser_urls(session)

    assert cdp_url == "https://9444-browser.sandbox.ppio.cn"
    assert live_url == ""
    assert headers == {"X-Access-Token": "temporary-ppio-token"}
