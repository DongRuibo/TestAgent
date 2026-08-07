"""公开网页 Browser Agent 工作流：提取、Artifact、Receipt 与确定性清理。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

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


@dataclass(frozen=True)
class BrowserWorkflowResult:
    success: bool
    source_url: str
    final_url: str | None
    status_code: int | None
    title: str | None
    text_preview: str | None
    text_total_chars: int
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
    ) -> BrowserWorkflowResult:
        validate_public_url(url)
        # 创建云实例前先做一次 DNS 地址分类；这是 fail-closed 预检，不能
        # 单独证明云端解析器在导航期间不会发生 rebinding。
        self._dns_resolver(url)
        if max_text_chars < 1:
            raise ValueError("max_text_chars must be positive.")

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
                        page.screenshot(type="png"),
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
            trust=trust,
            instructions_executable=instructions_executable,
            artifacts=tuple(output_records),
            receipt=receipt_record,
            cleanup=cleanup,
            error=receipt.error,
        )


def _sync_playwright() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _safe_error(message: str) -> str:
    message = re.sub(r"([?&]access_token=)[^&\s]+", r"\1[REDACTED]", message)
    return re.sub(r"(X-Access-Token['\":= ]+)[^,}\s]+", r"\1[REDACTED]", message)
