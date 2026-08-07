"""通过 Gateway HTTP 边界执行一次真实 Provider 端到端验证。

脚本默认拒绝云调用，必须显式传 ``--confirm-cloud``。每次只创建一个短时实例，
并把执行结果、Receipt 和清理状态写入 Gateway Artifact 目录。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sandbox_gateway.api import create_app
from sandbox_gateway.factory import build_auth, build_service


def _wait(client: TestClient, execution_id: str, token: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(
            f"/v1/executions/{execution_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 200:
            raise RuntimeError(f"query execution failed: {response.status_code} {response.text}")
        payload = response.json()
        if payload["status"] in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.2)
    raise TimeoutError(f"execution {execution_id} did not finish in {timeout}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one real Gateway Provider E2E case.")
    parser.add_argument("--provider", default="auto", choices=["auto", "tencent-agsx", "ppio-sandbox"])
    parser.add_argument(
        "--task",
        default="code",
        choices=["code", "sales", "spreadsheet", "briefing", "browser"],
    )
    parser.add_argument("--token", default="demo-token")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--confirm-cloud", action="store_true", help="Allow a real cloud sandbox call.")
    args = parser.parse_args()
    if not args.confirm_cloud:
        raise SystemExit("This command creates a billable cloud sandbox; re-run with --confirm-cloud.")

    service = build_service()
    app = create_app(service, build_auth())
    headers = {"Authorization": f"Bearer {args.token}"}
    started = time.perf_counter()
    try:
        with TestClient(app) as client:
            if args.task == "code":
                request = {
                    "task_type": "code",
                    "source": "print('gateway-provider-e2e')",
                    "provider_preference": args.provider,
                    "network_profile": "provider_default",
                }
            elif args.task in {"sales", "spreadsheet", "briefing"}:
                csv_path = PROJECT_ROOT / "fixtures" / "sales.csv"
                import base64

                uploaded = client.post(
                    "/v1/files",
                    headers=headers,
                    json={
                        "name": csv_path.name,
                        "media_type": "text/csv",
                        "content_base64": base64.b64encode(csv_path.read_bytes()).decode("ascii"),
                    },
                )
                if uploaded.status_code != 201:
                    raise RuntimeError(f"upload failed: {uploaded.status_code} {uploaded.text}")
                request = {
                    "task_type": "sales_analysis" if args.task == "sales" else "spreadsheet_analysis",
                    "input_files": [
                        {"file_id": uploaded.json()["file_id"], "remote_path": "/tmp/input/sales.csv"}
                    ],
                    "generate_briefing": args.task == "briefing",
                    "provider_preference": args.provider,
                }
            else:
                request = {
                    "task_type": "browser_public",
                    "url": "https://example.com",
                    "network_profile": "public_readonly",
                    "provider_preference": args.provider,
                }

            response = client.post(
                "/v1/executions",
                headers={**headers, "Idempotency-Key": f"gateway-e2e-{time.time_ns()}"},
                json=request,
            )
            if response.status_code != 202:
                raise RuntimeError(f"create execution failed: {response.status_code} {response.text}")
            execution_id = response.json()["execution_id"]
            result = _wait(client, execution_id, args.token, args.timeout)
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        service.shutdown()

    report = {
        "provider_requested": args.provider,
        "task": args.task,
        "execution_id": result["execution_id"],
        "provider_selected": result.get("provider"),
        "sandbox_id": result.get("sandbox_id"),
        "status": result["status"],
        "elapsed_ms": elapsed_ms,
        "result": result.get("result"),
        "receipt": result.get("receipt"),
        "error_type": result.get("error_type"),
        "error_message": result.get("error_message"),
    }
    output_dir = service.artifact_store.root / "e2e"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"gateway-e2e-{int(time.time())}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report={output}")
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
