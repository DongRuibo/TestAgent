"""用临时 AGSX Code 公网服务 + Browser 本地服务做 DNS rebinding 观测。

公网端和本地端都由本脚本创建并在 finally 中销毁；rbndr 只编码
127.0.0.1 与本次 Code 服务解析到的地址。该环境若无法访问 rbndr.us，
报告会是 inconclusive，而不是安全通过。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
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
from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--samples", type=int, default=4)
    parser.add_argument("--interval-seconds", type=float, default=0.5)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    if not 2 <= args.samples <= 12:
        parser.error("--samples must be between 2 and 12")
    if not 0 <= args.interval_seconds <= 10:
        parser.error("--interval-seconds must be between 0 and 10")

    load_dotenv(PROJECT_ROOT / ".env")
    config = TencentAGSXConfig.from_env(require_browser=True, default_browser_template="lazyx-browser-public-poc")
    provider = TencentAGSXProvider(config)
    store = ArtifactStore(PROJECT_ROOT / "artifacts")
    attempt_id = f"dns-controlled-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    report: dict = {
        "attempt_id": attempt_id,
        "status": "running",
        "scope": "controlled_agsx_code_public_endpoint_to_browser_local_endpoint",
        "observations": [],
        "limitations": [
            "This observes a DNS rebinding test path; it is not a complete provider policy proof.",
            "The current network resolver may proxy or block rbndr.us, producing inconclusive results.",
            "Only 127.0.0.1 is used as the local target; Metadata and RFC1918 addresses are not used.",
        ],
    }
    code_session = None
    browser_session = None
    try:
        code_context = RequestContext("dns-controlled-code", "test-user", "dns-controlled", f"{attempt_id}-code")
        code_session = provider.create_code(code_context, ttl_seconds=180)
        code_start = provider.run_command(
            code_session,
            f"python -m http.server {args.port} --bind 0.0.0.0 >/tmp/dns-public.log 2>&1 &",
            timeout_seconds=10,
        )
        public_host = code_session.native.get_host(args.port)
        public_ip = socket.getaddrinfo(public_host, args.port, type=socket.SOCK_STREAM)[0][4][0]
        public_hex = f"{int(ipaddress.ip_address(public_ip)):08x}"
        rbndr_host = f"7f000001.{public_hex}.rbndr.us"
        report.update(
            {
                "code_sandbox_id": code_session.sandbox_id,
                "public_host": public_host,
                "public_dns_ip": public_ip,
                "rbndr_host": rbndr_host,
                "code_server_start": code_start.to_dict(),
            }
        )

        browser_context = RequestContext("dns-controlled-browser", "test-user", "dns-controlled", f"{attempt_id}-browser")
        browser_session = provider.create_browser(browser_context, ttl_seconds=180)
        browser_start = provider.run_command(
            browser_session,
            f"python -m http.server {args.port} --bind 127.0.0.1 >/tmp/dns-local.log 2>&1 &",
            timeout_seconds=10,
        )
        report["browser_sandbox_id"] = browser_session.sandbox_id
        report["browser_local_server_start"] = browser_start.to_dict()

        from playwright.sync_api import sync_playwright

        url = f"http://{rbndr_host}:{args.port}/"
        report["url"] = url
        with sync_playwright() as playwright:
            cdp_url, _live_url, headers = provider.browser_urls(browser_session)
            browser = playwright.chromium.connect_over_cdp(cdp_url, headers=headers)
            try:
                context = browser.contexts[0]
                for index in range(args.samples):
                    page = context.new_page()
                    observation = {"sample": index + 1, "observed_at": datetime.now(UTC).isoformat()}
                    try:
                        response = page.goto(url, wait_until="commit", timeout=8_000)
                        observation["status_code"] = response.status if response else None
                        observation["server_addr"] = response.server_addr() if response else None
                        observation["final_url"] = page.url
                    except Exception as exc:
                        observation.update(
                            {
                                "error_type": type(exc).__name__,
                                "message": str(exc)[:300],
                                "final_url": page.url,
                            }
                        )
                    finally:
                        page.close()
                    report["observations"].append(observation)
                    if index + 1 < args.samples:
                        time.sleep(args.interval_seconds)
            finally:
                browser.close()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        cleanup = []
        if browser_session is not None:
            cleanup.append(provider.terminate(browser_session).to_dict())
        if code_session is not None:
            cleanup.append(provider.terminate(code_session).to_dict())
        report["cleanup"] = cleanup
        report["finished_at"] = datetime.now(UTC).isoformat()

    addresses = sorted(
        {
            str(item["server_addr"])
            for item in report["observations"]
            if item.get("server_addr")
        }
    )
    report["unique_server_addresses"] = addresses
    report["status"] = "multiple_server_addresses_observed" if len(addresses) > 1 else "inconclusive"
    report["decision"] = "MANUAL_REVIEW_REQUIRED"
    report["cleanup_passed"] = bool(report["cleanup"]) and all(
        item.get("destroyed") is True for item in report["cleanup"]
    )
    record = store.put_json(attempt_id, "controlled-dns-rebinding-report.json", report)
    print(json.dumps({"report": report, "artifact": record.to_dict()}, ensure_ascii=False, indent=2))
    return 0 if report["cleanup_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
