"""第一周 Code POC：多语言、文件、哈希、超时、断网与销毁报告。"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

# 允许按 `python scripts/...py` 直接运行，同时保持本地包可导入。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sandbox_runtime import ArtifactStore, RequestContext
from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider


PYTHON_PROBE = "print('PYTHON_OK:' + str(sum([2, 3, 5])))"
JAVASCRIPT_PROBE = "console.log('JAVASCRIPT_OK:' + [2, 3, 5].reduce((a, b) => a + b, 0))"
TIMEOUT_PROBE = "import time\ntime.sleep(10)\nprint('UNEXPECTED')"
NETWORK_PROBE = r'''
import json
import socket

result = {"public_internet_accessible": False}
try:
    addresses = socket.getaddrinfo("www.qq.com", 443, type=socket.SOCK_STREAM)
    result["dns_resolved"] = bool(addresses)
except Exception as exc:
    result["dns_resolved"] = False
    result["dns_error"] = {
        "error_type": type(exc).__name__,
        "message": str(exc)[:200],
    }

try:
    connection = socket.create_connection(("1.1.1.1", 443), timeout=3)
    connection.close()
    result["public_internet_accessible"] = True
except Exception as exc:
    result["tcp_error"] = {
        "error_type": type(exc).__name__,
        "message": str(exc)[:200],
    }
print(json.dumps(result, ensure_ascii=False))
'''


def main() -> int:
    load_dotenv()
    config = TencentAGSXConfig.from_env()
    provider = TencentAGSXProvider(config)
    attempt_id = f"code-poc-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    context = RequestContext("poc-tenant", "poc-user", "code-poc", attempt_id)
    store = ArtifactStore(Path("artifacts"))
    report: dict[str, object] = {
        "attempt_id": attempt_id,
        "provider": "tencent-agsx",
        "template": config.code_template,
        "expected_network_mode": "SANDBOX",
        "started_at": datetime.now(UTC).isoformat(),
        "checks": {},
        "status": "running",
    }
    session = None

    try:
        session = provider.create_code(context, ttl_seconds=300)
        report["sandbox_id"] = session.sandbox_id

        python_result = provider.execute_code(session, PYTHON_PROBE, language="python")
        report["checks"]["python"] = {
            "passed": python_result.success and "PYTHON_OK:10" in python_result.stdout,
            "duration_ms": python_result.duration_ms,
            "error_type": python_result.error_type,
        }

        javascript_result = provider.execute_code(
            session,
            JAVASCRIPT_PROBE,
            language="javascript",
        )
        report["checks"]["javascript"] = {
            "passed": javascript_result.success
            and "JAVASCRIPT_OK:10" in javascript_result.stdout,
            "duration_ms": javascript_result.duration_ms,
            "error_type": javascript_result.error_type,
            "error_message": javascript_result.error_message,
        }

        payload = f"lazyx-file-probe-{uuid4().hex}".encode("utf-8")
        remote_path = "/tmp/lazyx/code-poc/file-probe.bin"
        provider.put_file(session, remote_path, payload)
        downloaded = provider.get_file(session, remote_path)
        upload_hash = hashlib.sha256(payload).hexdigest()
        download_hash = hashlib.sha256(downloaded).hexdigest()
        report["checks"]["file_round_trip"] = {
            "passed": payload == downloaded,
            "size_bytes": len(downloaded),
            "upload_sha256": upload_hash,
            "download_sha256": download_hash,
        }

        network_result = provider.execute_code(
            session,
            NETWORK_PROBE,
            language="python",
            timeout_seconds=10,
        )
        network_payload = _last_json_object(network_result.stdout) if network_result.success else {}
        report["checks"]["sandbox_network"] = {
            "passed": network_result.success
            and network_payload.get("public_internet_accessible") is False,
            "duration_ms": network_result.duration_ms,
            "probe": network_payload,
            "error_type": network_result.error_type,
        }

        # 超时代码放在最后：某些 Jupyter Kernel 在客户端超时后仍会短暂占用
        # 当前执行上下文，若后面继续复用同一 Kernel 会污染其他探针耗时。
        timeout_result = provider.execute_code(
            session,
            TIMEOUT_PROBE,
            language="python",
            timeout_seconds=1,
        )
        report["checks"]["execution_timeout"] = {
            "passed": not timeout_result.success,
            "duration_ms": timeout_result.duration_ms,
            "error_type": timeout_result.error_type,
        }
    except Exception as exc:
        report["failure"] = f"{type(exc).__name__}: {_redact(str(exc), config.api_key)}"
    finally:
        if session is not None:
            cleanup = provider.terminate(session)
            report["cleanup"] = cleanup.to_dict()
        else:
            report["cleanup"] = {
                "sandbox_id": None,
                "destroyed": False,
                "error": "sandbox was not created",
            }
        report["finished_at"] = datetime.now(UTC).isoformat()

    checks = report["checks"]
    all_checks_passed = bool(checks) and all(
        bool(item.get("passed")) for item in checks.values()
    )
    cleanup_passed = bool(report["cleanup"].get("destroyed"))
    report["status"] = "passed" if all_checks_passed and cleanup_passed else "failed"
    record = store.put_json(attempt_id, "code-poc-report.json", report)
    print(json.dumps({"report": report, "artifact": record.to_dict()}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


def _last_json_object(stdout: str) -> dict[str, object]:
    for line in reversed([item.strip() for item in stdout.splitlines() if item.strip()]):
        value = json.loads(line)
        if isinstance(value, dict):
            return value
    raise ValueError("No JSON object found in stdout.")


def _redact(message: str, api_key: str) -> str:
    return message.replace(api_key, "[REDACTED]")


if __name__ == "__main__":
    sys.exit(main())
