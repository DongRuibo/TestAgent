"""DNS rebinding 实验入口的输入安全测试。"""

from __future__ import annotations

import pytest

from scripts.agsx_browser_dns_rebinding_poc import decode_rbndr_host


def test_decode_rbndr_host_accepts_loopback_and_public_probe() -> None:
    assert tuple(str(item) for item in decode_rbndr_host("7f000001.08080808.rbndr.us")) == (
        "127.0.0.1",
        "8.8.8.8",
    )


@pytest.mark.parametrize(
    "host",
    [
        "a9fea9fe.08080808.rbndr.us",  # 169.254.169.254
        "0a000001.08080808.rbndr.us",  # 10.0.0.1
        "7f000001.08080808.example.com",
    ],
)
def test_decode_rbndr_host_rejects_sensitive_or_uncontrolled_hosts(host: str) -> None:
    with pytest.raises(ValueError):
        decode_rbndr_host(host)
