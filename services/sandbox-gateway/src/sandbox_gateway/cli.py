"""Sandbox Gateway 命令行入口。"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.getenv("SANDBOX_GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("SANDBOX_GATEWAY_PORT", "8787"))
    uvicorn.run("sandbox_gateway.factory:app", host=host, port=port)


if __name__ == "__main__":
    main()
