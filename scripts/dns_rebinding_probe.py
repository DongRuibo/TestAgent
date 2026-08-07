"""对受控 rbndr.us 主机做 DNS 轮询观测；不访问解析出的地址。

该脚本只证明测试域名确实可能在多个 DNS 答案之间切换，不能证明云端
Browser 的导航路径已经具备 rebinding 防护。默认建议使用 127.0.0.1 与
8.8.8.8 这样的无敏感目标，不要使用 Metadata 地址。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import time
from datetime import UTC, datetime


def resolve(host: str) -> list[str]:
    values = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(item[4][0]) for item in values if item[4]})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if args.samples < 2 or args.samples > 30:
        parser.error("--samples must be between 2 and 30")
    if args.interval_seconds < 0 or args.interval_seconds > 10:
        parser.error("--interval-seconds must be between 0 and 10")
    if not args.host.endswith(".rbndr.us"):
        parser.error("仅允许使用 rbndr.us 受控测试域名")

    observations = []
    for index in range(args.samples):
        try:
            addresses = resolve(args.host)
            private = [
                value
                for value in addresses
                if ipaddress.ip_address(value).is_private
                or ipaddress.ip_address(value).is_loopback
            ]
            observations.append(
                {
                    "sample": index + 1,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "addresses": addresses,
                    "private_or_loopback": private,
                }
            )
        except OSError as exc:
            observations.append(
                {
                    "sample": index + 1,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:300],
                }
            )
        if index + 1 < args.samples:
            time.sleep(args.interval_seconds)

    addresses = sorted(
        {value for item in observations for value in item.get("addresses", [])}
    )
    print(
        json.dumps(
            {
                "host": args.host,
                "status": "multiple_answers_observed" if len(addresses) > 1 else "inconclusive",
                "unique_addresses": addresses,
                "observations": observations,
                "scope": "control_plane_dns_only",
                "limitation": "does not exercise the AGSX Browser resolver or navigation TOCTOU path",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if len(addresses) > 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
