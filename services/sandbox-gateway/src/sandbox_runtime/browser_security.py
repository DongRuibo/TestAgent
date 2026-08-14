"""Browser POC 的 URL 安全分类与不可信内容模型。"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse


_LOCAL_DNS_PROXY_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@dataclass(frozen=True)
class BrowserObservation:
    source_url: str
    title: str
    text: str
    trust: str = "untrusted"
    instructions_executable: bool = False


def validate_public_url(url: str) -> None:
    """在发给浏览器前拒绝明显的本地、私网与 Metadata 目标。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in URLs are not allowed.")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must contain a hostname.")
    if hostname.lower() == "localhost":
        raise ValueError("Localhost is forbidden.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if is_forbidden_address(address):
        raise ValueError(f"Forbidden destination address: {hostname}")


def resolve_public_url(
    url: str,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
) -> tuple[str, ...]:
    """解析 URL 主机并拒绝 DNS 返回的本地、私网或 Metadata 地址。

    这是浏览器启动前的单次预检，能阻止普通域名解析到内网的情况；它不能
    单独证明云端浏览器具备完整的 DNS rebinding 防护，因此调用方仍需在
    导航后重新校验最终 URL，并把该限制写入测试报告。
    """
    validate_public_url(url)
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:  # validate_public_url 已覆盖，这里只为类型收窄
        raise ValueError("URL must contain a hostname.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    lookup = resolver or socket.getaddrinfo
    try:
        infos = lookup(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise ValueError(f"DNS resolution failed for {hostname}: {exc}") from exc

    addresses = tuple(
        sorted(
            {
                str(info[4][0])
                for info in infos
                if len(info) > 4 and info[4] and info[4][0]
            }
        )
    )
    if not addresses:
        raise ValueError(f"DNS resolution returned no addresses for {hostname}")
    forbidden = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        # 当前开发环境的 DNS 出口会把公网解析结果映射到 RFC  Reserved
        # benchmark 网段 198.18.0.0/15；它不是目标内网地址，不能把所有
        # 公网页面误判为私网。真正的 loopback、RFC1918、link-local 和
        # Metadata 地址仍然 fail closed。
        if is_forbidden_address(address) and address not in _LOCAL_DNS_PROXY_NETWORK:
            forbidden.append(value)
    if forbidden:
        raise ValueError(
            f"DNS resolved {hostname} to forbidden destination(s): {', '.join(forbidden)}"
        )
    return addresses


def is_forbidden_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )
