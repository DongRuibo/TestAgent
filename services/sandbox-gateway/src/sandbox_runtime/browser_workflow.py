"""公开网页 Browser Agent 工作流：提取、Artifact、Receipt 与确定性清理。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Callable
from uuid import uuid4

from PIL import Image

from sandbox_runtime.artifact_store import ArtifactStore
from sandbox_runtime.browser_security import (
    BrowserObservation,
    resolve_public_url,
    validate_public_url,
)
from sandbox_runtime.models import (
    ArtifactRecord,
    CleanupResult,
    ExecutionReceipt,
    RequestContext,
    SandboxSession,
    utc_now,
)
from sandbox_runtime.provider import SandboxProvider, provider_name


PlaywrightFactory = Callable[[], Any]
DEFAULT_MAX_SCREENSHOT_HEIGHT = 20_000
DEFAULT_MAX_SCROLL_STEPS = 50
DEFAULT_SCROLL_WAIT_MS = 150
PAGE_METRICS_SCRIPT = """() => {
    const body = document.body;
    const root = document.documentElement;
    return {
        pageHeight: Math.max(
            body ? body.scrollHeight : 0,
            body ? body.offsetHeight : 0,
            root ? root.scrollHeight : 0,
            root ? root.offsetHeight : 0,
            window.innerHeight
        ),
        viewportHeight: Math.max(window.innerHeight, 1),
        viewportWidth: Math.max(window.innerWidth, 1)
    };
}"""


@dataclass(frozen=True)
class BrowserWorkflowResult:
    success: bool
    source_url: str
    final_url: str | None
    status_code: int | None
    title: str | None
    text_preview: str | None
    text_total_chars: int
    page_height: int | None
    screenshot_height: int | None
    screenshot_full_page: bool
    screenshot_truncated: bool
    screenshot_stitched: bool
    screenshot_scroll_steps: int
    trust: str
    instructions_executable: bool
    artifacts: tuple[ArtifactRecord, ...]
    receipt: ArtifactRecord
    cleanup: CleanupResult | None
    error: str | None = None


class PublicBrowserWorkflow:
    """在一次性 Browser Sandbox 中读取一个公开网页。"""

    def __init__(
        self,
        provider: SandboxProvider,
        artifact_store: ArtifactStore,
        *,
        playwright_factory: PlaywrightFactory | None = None,
        dns_resolver: Callable[[str], tuple[str, ...]] | None = None,
    ) -> None:
        self.provider = provider
        self.artifact_store = artifact_store
        self._playwright_factory = playwright_factory or _sync_playwright
        self._dns_resolver = dns_resolver or resolve_public_url

    def run(
        self,
        url: str,
        context: RequestContext,
        *,
        max_text_chars: int = 12_000,
        max_screenshot_height: int = DEFAULT_MAX_SCREENSHOT_HEIGHT,
        max_scroll_steps: int = DEFAULT_MAX_SCROLL_STEPS,
        scroll_wait_ms: int = DEFAULT_SCROLL_WAIT_MS,
    ) -> BrowserWorkflowResult:
        validate_public_url(url)
        # 创建云实例前先做一次 DNS 地址分类；这是 fail-closed 预检，不能
        # 单独证明云端解析器在导航期间不会发生 rebinding。
        self._dns_resolver(url)
        if max_text_chars < 1:
            raise ValueError("max_text_chars must be positive.")
        if max_screenshot_height < 1:
            raise ValueError("max_screenshot_height must be positive.")
        if max_scroll_steps < 1:
            raise ValueError("max_scroll_steps must be positive.")
        if scroll_wait_ms < 0:
            raise ValueError("scroll_wait_ms cannot be negative.")

        receipt = ExecutionReceipt(
            receipt_id=f"receipt-{uuid4().hex}",
            context=context,
            provider=provider_name(self.provider),
            action="browser_public_extract",
            started_at=utc_now(),
            network_destinations=[url],
        )
        session: SandboxSession | None = None
        cleanup: CleanupResult | None = None
        output_records: list[ArtifactRecord] = []
        final_url: str | None = None
        status_code: int | None = None
        title: str | None = None
        text_preview: str | None = None
        text_total_chars = 0
        page_height: int | None = None
        screenshot_height: int | None = None
        screenshot_full_page = False
        screenshot_truncated = False
        screenshot_stitched = False
        screenshot_scroll_steps = 0
        trust = "untrusted"
        instructions_executable = False

        try:
            # 云实例先创建成功，再启动本地 Playwright，避免创建失败时留下后台任务。
            session = self.provider.create_browser(context, ttl_seconds=300)
            receipt.sandbox_id = session.sandbox_id
            cdp_url, _live_url, headers = self.provider.browser_urls(session)
            with self._playwright_factory() as playwright:
                browser = playwright.chromium.connect_over_cdp(cdp_url, headers=headers)
                try:
                    browser_context = browser.contexts[0]
                    page = (
                        browser_context.pages[0]
                        if browser_context.pages
                        else browser_context.new_page()
                    )
                    response = page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                    if response is None:
                        raise RuntimeError("Browser navigation returned no HTTP response.")
                    final_url = page.url
                    # 重定向后再次校验；DNS 预解析仍不能消除云端 TOCTOU。
                    validate_public_url(final_url)
                    self._dns_resolver(final_url)
                    status_code = response.status
                    title = page.title()
                    capture = _capture_long_screenshot(
                        page,
                        max_height=max_screenshot_height,
                        max_scroll_steps=max_scroll_steps,
                        scroll_wait_ms=scroll_wait_ms,
                    )
                    page_height = capture.page_height
                    screenshot_height = capture.screenshot_height
                    screenshot_full_page = capture.full_page
                    screenshot_truncated = capture.truncated
                    screenshot_stitched = capture.stitched
                    screenshot_scroll_steps = capture.scroll_steps
                    text = page.locator("body").inner_text(timeout=5_000)
                    text_total_chars = len(text)
                    text_preview = text[:max_text_chars]
                    observation = BrowserObservation(final_url, title, text_preview)
                    trust = observation.trust
                    instructions_executable = observation.instructions_executable

                    text_record = self.artifact_store.put_bytes(
                        context.attempt_id,
                        "page-content.txt",
                        text.encode("utf-8"),
                        "text/plain",
                    )
                    screenshot_record = self.artifact_store.put_bytes(
                        context.attempt_id,
                        "page-screenshot.png",
                        capture.data,
                        "image/png",
                    )
                    output_records.extend((text_record, screenshot_record))
                    receipt.outputs.extend(output_records)
                    if final_url != url:
                        receipt.network_destinations.append(final_url)
                    receipt.status = "passed"
                finally:
                    browser.close()
        except Exception as exc:
            receipt.status = "failed"
            receipt.error = f"{type(exc).__name__}: {_safe_error(str(exc))}"
        finally:
            if session is not None:
                cleanup = self.provider.terminate(session)
                receipt.cleanup = cleanup
                if not cleanup.destroyed:
                    receipt.status = "failed"
                    detail = cleanup.error or "sandbox was not destroyed"
                    receipt.error = receipt.error or f"CleanupError: {detail}"
            receipt.finished_at = utc_now()

        receipt_record = self.artifact_store.put_json(
            context.attempt_id,
            "execution-receipt.json",
            receipt.to_dict(),
        )
        return BrowserWorkflowResult(
            success=receipt.status == "passed",
            source_url=url,
            final_url=final_url,
            status_code=status_code,
            title=title,
            text_preview=text_preview,
            text_total_chars=text_total_chars,
            page_height=page_height,
            screenshot_height=screenshot_height,
            screenshot_full_page=screenshot_full_page,
            screenshot_truncated=screenshot_truncated,
            screenshot_stitched=screenshot_stitched,
            screenshot_scroll_steps=screenshot_scroll_steps,
            trust=trust,
            instructions_executable=instructions_executable,
            artifacts=tuple(output_records),
            receipt=receipt_record,
            cleanup=cleanup,
            error=receipt.error,
        )


@dataclass(frozen=True)
class _ScreenshotCapture:
    data: bytes
    page_height: int
    screenshot_height: int
    full_page: bool
    truncated: bool
    stitched: bool
    scroll_steps: int


def _capture_long_screenshot(
    page: Any,
    *,
    max_height: int,
    max_scroll_steps: int,
    scroll_wait_ms: int,
) -> _ScreenshotCapture:
    metrics = _page_metrics(page)
    target_height = min(metrics["page_height"], max_height)
    next_scroll = metrics["viewport_height"]
    scroll_steps = 0

    while next_scroll < target_height and scroll_steps < max_scroll_steps:
        page.evaluate("(scrollY) => window.scrollTo(0, scrollY)", next_scroll)
        if scroll_wait_ms:
            page.wait_for_timeout(scroll_wait_ms)
        scroll_steps += 1
        metrics = _page_metrics(page)
        target_height = min(metrics["page_height"], max_height)
        next_scroll += metrics["viewport_height"]

    scroll_limit_reached = (
        scroll_steps >= max_scroll_steps and next_scroll < target_height
    )
    page.evaluate("() => window.scrollTo(0, 0)")
    if scroll_wait_ms:
        page.wait_for_timeout(scroll_wait_ms)
    metrics = _page_metrics(page)
    page_height = metrics["page_height"]
    screenshot_height = min(page_height, max_height)
    truncated = page_height > max_height or scroll_limit_reached
    screenshot_options: dict[str, Any] = {
        "type": "png",
        "animations": "disabled",
        "caret": "hide",
        "scale": "css",
    }
    if not truncated:
        screenshot_options["full_page"] = True
        screenshot_data = page.screenshot(**screenshot_options)
        _width, actual_height = _image_size(screenshot_data)
        if actual_height >= screenshot_height:
            return _ScreenshotCapture(
                data=screenshot_data,
                page_height=page_height,
                screenshot_height=actual_height,
                full_page=True,
                truncated=False,
                stitched=False,
                scroll_steps=scroll_steps,
            )

    screenshot_data = _stitch_viewport_screenshots(
        page,
        capture_height=screenshot_height,
        viewport_height=metrics["viewport_height"],
        scroll_wait_ms=scroll_wait_ms,
        screenshot_options=screenshot_options,
    )
    _width, actual_height = _image_size(screenshot_data)
    return _ScreenshotCapture(
        data=screenshot_data,
        page_height=page_height,
        screenshot_height=actual_height,
        full_page=not truncated,
        truncated=truncated,
        stitched=True,
        scroll_steps=scroll_steps,
    )


def _stitch_viewport_screenshots(
    page: Any,
    *,
    capture_height: int,
    viewport_height: int,
    scroll_wait_ms: int,
    screenshot_options: dict[str, Any],
) -> bytes:
    last_position = max(capture_height - viewport_height, 0)
    positions = list(range(0, last_position, viewport_height))
    positions.append(last_position)
    positions = list(dict.fromkeys(positions))
    canvas: Image.Image | None = None
    css_to_pixel = 1.0
    try:
        for position in positions:
            page.evaluate("(scrollY) => window.scrollTo(0, scrollY)", position)
            if scroll_wait_ms:
                page.wait_for_timeout(scroll_wait_ms)
            segment_data = page.screenshot(**screenshot_options)
            with Image.open(BytesIO(segment_data)) as source:
                segment = source.convert("RGB")
                if canvas is None:
                    css_to_pixel = segment.height / viewport_height
                    canvas = Image.new(
                        "RGB",
                        (
                            segment.width,
                            max(1, round(capture_height * css_to_pixel)),
                        ),
                        "white",
                    )
                paste_y = round(position * css_to_pixel)
                remaining = canvas.height - paste_y
                if remaining <= 0:
                    break
                crop_height = min(segment.height, remaining)
                canvas.paste(segment.crop((0, 0, segment.width, crop_height)), (0, paste_y))
    finally:
        page.evaluate("() => window.scrollTo(0, 0)")
        if scroll_wait_ms:
            page.wait_for_timeout(scroll_wait_ms)
    if canvas is None:
        raise RuntimeError("Browser produced no viewport screenshots.")
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _image_size(data: bytes) -> tuple[int, int]:
    try:
        with Image.open(BytesIO(data)) as image:
            return image.size
    except Exception as exc:
        raise RuntimeError("Browser screenshot is not a valid image.") from exc


def _page_metrics(page: Any) -> dict[str, int]:
    raw = page.evaluate(PAGE_METRICS_SCRIPT)
    if not isinstance(raw, dict):
        raise RuntimeError("Browser page metrics returned a non-object value.")
    try:
        page_height = max(1, int(raw["pageHeight"]))
        viewport_height = max(1, int(raw["viewportHeight"]))
        viewport_width = max(1, int(raw["viewportWidth"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Browser page metrics are invalid.") from exc
    return {
        "page_height": page_height,
        "viewport_height": viewport_height,
        "viewport_width": viewport_width,
    }


def _sync_playwright() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _safe_error(message: str) -> str:
    message = re.sub(r"([?&]access_token=)[^&\s]+", r"\1[REDACTED]", message)
    return re.sub(r"(X-Access-Token['\":= ]+)[^,}\s]+", r"\1[REDACTED]", message)
