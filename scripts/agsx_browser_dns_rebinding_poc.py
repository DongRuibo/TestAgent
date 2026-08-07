"""在 AGSX Browser 中观测受控 rbndr.us 主机的 DNS/连接变化。

脚本只接受 rbndr.us 格式的两个 IPv4 地址，禁止 Metadata/RFC1918 目标；
它记录 Playwright response.server_addr()，但不会把“只看到一个答案”算作
安全通过。要证明 TOCTOU 防护，需要多次看到受控公网服务和本地测试服务的
连接行为，并结合 Provider 网络日志复核。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sandbox_runtime import ArtifactStore, RequestContext
from sandbox_runtime.models import SandboxSession
from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider


def decode_rbndr_host(host: str) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]:
    labels = host.rstrip(".").lower().split(".")
    if len(labels) != 4 or labels[2] != "rbndr" or labels[3] != "us":
        raise ValueError("host must have the form <hex-ip>.<hex-ip>.rbndr.us")
    try:
        values = tuple(ipaddress.IPv4Address(int(label, 16)) for label in labels[:2])
    except (ValueError, OverflowError) as exc:
        raise ValueError("rbndr IPv4 labels must be hexadecimal dwords") from exc
    forbidden = ipaddress.ip_address("169.254.169.254")
    for address in values:
        if address == forbidden or address.is_private and not address.is_loopback:
            raise ValueError(f"refusing sensitive rebinding target: {address}")
    return values  # type: ignore[return-value]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--report-root", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not 2 <= args.samples <= 30:
        parser.error("--samples must be between 2 and 30")
    if not 0 <= args.interval_seconds <= 10:
        parser.error("--interval-seconds must be between 0 and 10")
    try:
        target_addresses = decode_rbndr_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))

    load_dotenv(PROJECT_ROOT / ".env")
    config = TencentAGSXConfig.from_env(
        require_browser=True,
        default_browser_template="lazyx-browser-public-poc",
    )
    provider = TencentAGSXProvider(config)
    store = ArtifactStore(args.report_root)
    attempt_id = f"browser-dns-rebinding-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    context = RequestContext("dns-poc-tenant", "dns-poc-user", "dns-rebinding", attempt_id)
    url = f"http://{args.host}:{args.port}/"
    report: dict = {
        "attempt_id": attempt_id,
        "host": args.host,
        "url": url,
        "encoded_addresses": [str(item) for item in target_addresses],
        "samples": args.samples,
        "status": "running",
        "scope": "cloud_browser_navigation_observation",
        "limitations": [
            "This is an observation harness, not a proof of complete DNS rebinding protection.",
            "A controlled public endpoint and a controlled local/private test service are required.",
            "Do not use Metadata or production internal addresses as rbndr targets.",
        ],
        "observations": [],
    }
    session: SandboxSession | None = None
    cleanup = None
    try:
        from playwright.sync_api import sync_playwright

        session = provider.create_browser(context, ttl_seconds=300)
        report["sandbox_id"] = session.sandbox_id
        with sync_playwright() as playwright:
            cdp_url, _live_url, headers = provider.browser_urls(session)
            browser = playwright.chromium.connect_over_cdp(cdp_url, headers=headers)
            try:
                browser_context = browser.contexts[0]
                for index in range(args.samples):
                    page = browser_context.new_page()
                    observation = {
                        "sample": index + 1,
                        "observed_at": datetime.now(UTC).isoformat(),
                    }
                    try:
                        response = page.goto(url, wait_until="commit", timeout=8_000)
                        observation["status_code"] = response.status if response else None
                        if response:
                            try:
                                observation["server_addr"] = response.server_addr()
                            except Exception:
                                observation["server_addr"] = None
                        observation["final_url"] = _safe_url(page.url)
                    except Exception as exc:
                        observation["error_type"] = type(exc).__name__
                        observation["message"] = _sanitize(str(exc))[:300]
                        observation["final_url"] = _safe_url(page.url)
                    finally:
                        page.close()
                    report["observations"].append(observation)
                    if index + 1 < args.samples:
                        time.sleep(args.interval_seconds)
            finally:
                browser.close()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {_sanitize(str(exc))}"
    finally:
        if session is not None:
            cleanup = provider.terminate(session)
            report["cleanup"] = cleanup.to_dict()
        report["finished_at"] = datetime.now(UTC).isoformat()

    observed = sorted(
        {
            str(item["server_addr"])
            for item in report["observations"]
            if item.get("server_addr")
        }
    )
    report["unique_server_addresses"] = observed
    report["status"] = (
        "multiple_server_addresses_observed" if len(observed) > 1 else "inconclusive"
    )
    report["decision"] = "MANUAL_REVIEW_REQUIRED"
    record = store.put_json(attempt_id, "dns-rebinding-report.json", report)
    print(json.dumps({"report": report, "artifact": record.to_dict()}, ensure_ascii=False, indent=2))
    return 0 if report.get("cleanup", {}).get("destroyed") is True else 1


def _safe_url(url: str) -> str:
    return re.sub(r"([?&]access_token=)[^&\s]+", r"\1[REDACTED]", url)


def _sanitize(message: str) -> str:
    message = re.sub(r"([?&]access_token=)[^&\s]+", r"\1[REDACTED]", message)
    return re.sub(r"(X-Access-Token['\":= ]+)[^,}\s]+", r"\1[REDACTED]", message)


if __name__ == "__main__":
    raise SystemExit(main())
