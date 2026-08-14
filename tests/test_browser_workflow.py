"""公开网页 Browser 工作流的 Artifact、Receipt 与清理测试。"""

from __future__ import annotations

import json
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

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

    def __init__(
        self,
        *,
        page_heights: tuple[int, ...] = (2_400,),
        viewport_height: int = 800,
        viewport_width: int = 1_200,
        ignore_full_page: bool = False,
    ) -> None:
        self.page_heights = page_heights
        self.viewport_height = viewport_height
        self.viewport_width = viewport_width
        self.ignore_full_page = ignore_full_page
        self.metrics_calls = 0
        self.scroll_positions: list[int] = []
        self.waits: list[int] = []
        self.screenshot_options: list[dict] = []

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

    def evaluate(self, expression: str, argument=None):
        if "window.scrollTo" in expression:
            self.scroll_positions.append(0 if argument is None else argument)
            return None
        height = self.page_heights[
            min(self.metrics_calls, len(self.page_heights) - 1)
        ]
        self.metrics_calls += 1
        return {
            "pageHeight": height,
            "viewportHeight": self.viewport_height,
            "viewportWidth": self.viewport_width,
        }

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)

    def screenshot(self, **options) -> bytes:
        self.screenshot_options.append(options)
        assert options["type"] == "png"
        height = (
            self.viewport_height
            if self.ignore_full_page or not options.get("full_page")
            else self.page_heights[-1]
        )
        output = BytesIO()
        Image.new("RGB", (self.viewport_width, height), "white").save(
            output,
            format="PNG",
        )
        return output.getvalue()


class FakeBrowser:
    def __init__(self, page: FakePage | None = None) -> None:
        self.page = page or FakePage()
        self.contexts = [
            SimpleNamespace(pages=[self.page], new_page=lambda: self.page)
        ]
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
    assert result.page_height == 2_400
    assert result.screenshot_height == 2_400
    assert result.screenshot_full_page is True
    assert result.screenshot_truncated is False
    assert result.screenshot_stitched is False
    assert result.screenshot_scroll_steps == 2
    assert browser.page.scroll_positions == [800, 1_600, 0]
    assert browser.page.screenshot_options == [{
        "type": "png",
        "animations": "disabled",
        "caret": "hide",
        "scale": "css",
        "full_page": True,
    }]
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


def test_public_browser_workflow_scrolls_again_when_lazy_page_grows(tmp_path) -> None:
    provider = FakeBrowserProvider()
    page = FakePage(page_heights=(1_600, 3_200))
    browser = FakeBrowser(page)
    workflow = PublicBrowserWorkflow(
        provider,
        ArtifactStore(tmp_path / "artifacts"),
        playwright_factory=lambda: FakePlaywrightManager(browser),
        dns_resolver=lambda _url: ("93.184.216.34",),
    )

    result = workflow.run("https://example.com", RequestContext("t", "u", "x", "lazy"))

    assert result.success is True
    assert result.page_height == 3_200
    assert result.screenshot_full_page is True
    assert result.screenshot_truncated is False
    assert result.screenshot_stitched is False
    assert result.screenshot_scroll_steps == 3
    assert page.scroll_positions == [800, 1_600, 2_400, 0]


def test_public_browser_workflow_caps_extremely_tall_screenshot(tmp_path) -> None:
    provider = FakeBrowserProvider()
    page = FakePage(page_heights=(5_000,))
    browser = FakeBrowser(page)
    workflow = PublicBrowserWorkflow(
        provider,
        ArtifactStore(tmp_path / "artifacts"),
        playwright_factory=lambda: FakePlaywrightManager(browser),
        dns_resolver=lambda _url: ("93.184.216.34",),
    )

    result = workflow.run(
        "https://example.com",
        RequestContext("t", "u", "x", "tall"),
        max_screenshot_height=2_000,
        scroll_wait_ms=0,
    )

    assert result.success is True
    assert result.page_height == 5_000
    assert result.screenshot_height == 2_000
    assert result.screenshot_full_page is False
    assert result.screenshot_truncated is True
    assert result.screenshot_stitched is True
    assert result.screenshot_scroll_steps == 2
    assert page.scroll_positions == [800, 1_600, 0, 0, 800, 1_200, 0]
    assert len(page.screenshot_options) == 3
    assert all("full_page" not in item for item in page.screenshot_options)


def test_public_browser_workflow_stitches_when_remote_ignores_full_page(tmp_path) -> None:
    provider = FakeBrowserProvider()
    page = FakePage(page_heights=(2_400,), ignore_full_page=True)
    browser = FakeBrowser(page)
    store = ArtifactStore(tmp_path / "artifacts")
    workflow = PublicBrowserWorkflow(
        provider,
        store,
        playwright_factory=lambda: FakePlaywrightManager(browser),
        dns_resolver=lambda _url: ("93.184.216.34",),
    )

    result = workflow.run("https://example.com", RequestContext("t", "u", "x", "fallback"))

    assert result.success is True
    assert result.page_height == 2_400
    assert result.screenshot_height == 2_400
    assert result.screenshot_full_page is True
    assert result.screenshot_truncated is False
    assert result.screenshot_stitched is True
    screenshot = next(item for item in result.artifacts if item.name == "page-screenshot.png")
    with Image.open(BytesIO(store.read_bytes(screenshot))) as image:
        assert image.size == (1_200, 2_400)
