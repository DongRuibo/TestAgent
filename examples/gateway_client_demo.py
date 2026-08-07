"""用 Gateway SDK 展示可接入其他 Agent 的核心沙箱工具。"""

from __future__ import annotations

import os

from sandbox_client import SandboxClient


def main() -> None:
    base_url = os.getenv("SANDBOX_GATEWAY_URL", "http://127.0.0.1:8787")
    api_key = os.getenv("SANDBOX_GATEWAY_API_KEY", "demo-token")
    with SandboxClient(base_url, api_key) as sandbox:
        code = sandbox.execute_code(
            "print('hello from another Agent')",
            provider_preference=os.getenv("SANDBOX_PROVIDER", "auto"),
        )
        print("[code]", code.status.value, code.result)

        spreadsheet = sandbox.analyze_spreadsheet_file(
            "operations.csv",
            b"date,department,amount\n2026-08-01,Design,100\n",
            generate_briefing=True,
        )
        print("[spreadsheet]", spreadsheet.status.value, spreadsheet.result)
        print("[artifacts]", [item.name for item in spreadsheet.artifacts])

        page = sandbox.browse_public_page("https://example.com")
        print("[browser]", page.status.value, page.result)
        print("[cleanup]", page.receipt.get("cleanup") if page.receipt else None)


if __name__ == "__main__":
    main()
