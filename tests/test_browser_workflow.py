"""公开网页 Browser 工作流的 Artifact、Receipt 与清理测试。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from sandbox_runtime.artifact_store import ArtifactStore
from sandbox_runtime.browser_workflow import PublicBrowserWorkflow
from sandbox_runtime.models import (
    CleanupResult,
    RequestContext,
    SandboxKind,
    SandboxSession,
    utc_now,
)


class FakePage:
    url = "https://example.com/final"

    def goto(self, url: str, *, wait_until: str, timeout: int):
        assert url == "https://example.com"
        assert wait_until == "domcontentloaded"
        assert timeout == 20_000
        return SimpleNamespace(status=200)

    def title(self) -> str:
        return "Example"

    def locator(self, selector: str):
        assert selector == "body"
        return SimpleNamespace(inner_text=lambda timeout: "public page text")

    def screenshot(self, *, type: str) -> bytes:
        assert type == "png"
        return b"fake-png"


class FakeBrowser:
    def __init__(self) -> None:
        page = FakePage()
        self.contexts = [SimpleNamespace(pages=[page], new_page=lambda: page)]
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakePlaywrightManager:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser

    def __enter__(self):
        chromium = SimpleNamespace(
            connect_over_cdp=lambda url, headers: self.browser,
        )
        return SimpleNamespace(chromium=chromium)

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeBrowserProvider:
    def __init__(self) -> None:
        self.terminated = False

    def create_browser(self, context: RequestContext, ttl_seconds: int = 300):
        now = utc_now()
        return SandboxSession(
            sandbox_id="sbi-browser",
            provider="fake",
            kind=SandboxKind.BROWSER,
            template="browser-tool",
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            metadata=context.metadata(),
            native=object(),
        )

    def browser_urls(self, session: SandboxSession):
        return (
            "https://sandbox.example.com/cdp?access_token=secret",
            "https://sandbox.example.com/live?access_token=secret",
            {"X-Access-Token": "secret"},
        )

    def terminate(self, session: SandboxSession) -> CleanupResult:
        self.terminated = True
        return CleanupResult(session.sandbox_id, True)


def test_public_browser_workflow_persists_outputs_receipt_and_cleanup(
    tmp_path: Path,
) -> None:
    provider = FakeBrowserProvider()
    browser = FakeBrowser()
    store = ArtifactStore(tmp_path / "artifacts")
    workflow = PublicBrowserWorkflow(
        provider,
        store,
        playwright_factory=lambda: FakePlaywrightManager(browser),
        dns_resolver=lambda _url: ("93.184.216.34",),
    )
    context = RequestContext("tenant", "user", "task", "attempt-browser")

    result = workflow.run("https://example.com", context)

    assert result.success is True
    assert result.title == "Example"
    assert result.text_preview == "public page text"
    assert result.trust == "untrusted"
    assert result.instructions_executable is False
    assert {item.name for item in result.artifacts} == {
        "page-content.txt",
        "page-screenshot.png",
    }
    assert browser.closed is True
    assert provider.terminated is True
    receipt = json.loads(store.read_bytes(result.receipt))
    assert receipt["status"] == "passed"
    assert receipt["cleanup"]["destroyed"] is True
    assert receipt["network_destinations"] == [
        "https://example.com",
        "https://example.com/final",
    ]
