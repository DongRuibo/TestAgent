"""AGSX Code 的跨租户文件隔离、超时清理与 Receipt 关联 POC。"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sandbox_runtime import ArtifactStore, RequestContext
from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider


REMOTE_MARKER = "/tmp/lazyx/isolation/marker.txt"
READ_MARKER_CODE = (
    "from pathlib import Path\n"
    f"print(Path({REMOTE_MARKER!r}).read_text())"
)


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    config = TencentAGSXConfig.from_env()
    provider = TencentAGSXProvider(config)
    store = ArtifactStore(PROJECT_ROOT / "artifacts")
    attempt_id = f"isolation-poc-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    contexts = [
        RequestContext("tenant-a", "user-a", "isolation-poc", f"{attempt_id}-a"),
        RequestContext("tenant-b", "user-b", "isolation-poc", f"{attempt_id}-b"),
    ]
    sessions = []
    report: dict = {
        "attempt_id": attempt_id,
        "provider": "tencent-agsx",
        "status": "running",
        "checks": {},
        "sessions": [],
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        for context, marker in zip(contexts, ("TENANT_A_MARKER", "TENANT_B_MARKER")):
            session = provider.create_code(context, ttl_seconds=300)
            sessions.append(session)
            provider.put_file(session, REMOTE_MARKER, marker.encode("utf-8"))
            execution = provider.execute_code(session, READ_MARKER_CODE, timeout_seconds=10)
            report["sessions"].append(
                {
                    "sandbox_id": session.sandbox_id,
                    "tenant_id": context.tenant_id,
                    "user_id": context.user_id,
                    "attempt_id": context.attempt_id,
                    "metadata": session.metadata,
                    "observed_marker": execution.stdout.strip(),
                    "execution_success": execution.success,
                }
            )

        markers_are_isolated = [
            item["observed_marker"] == expected
            for item, expected in zip(report["sessions"], ("TENANT_A_MARKER", "TENANT_B_MARKER"))
        ]
        report["checks"]["cross_tenant_file_isolation"] = {
            "passed": all(markers_are_isolated),
            "details": "same remote path returned tenant-specific content in separate instances",
        }
        report["checks"]["metadata_tenant_binding"] = {
            "passed": all(
                item["metadata"].get("tenant_id") == item["tenant_id"]
                and item["metadata"].get("user_id") == item["user_id"]
                and item["metadata"].get("attempt_id") == item["attempt_id"]
                for item in report["sessions"]
            )
        }

        timeout_context = RequestContext(
            "tenant-timeout", "user-timeout", "isolation-timeout", f"{attempt_id}-timeout"
        )
        timeout_session = provider.create_code(timeout_context, ttl_seconds=300)
        sessions.append(timeout_session)
        timeout_result = provider.execute_code(
            timeout_session,
            "import time\ntime.sleep(10)",
            timeout_seconds=1,
        )
        report["checks"]["timeout_failure_classification"] = {
            "passed": not timeout_result.success
            and timeout_result.error_type == "EXECUTION_TIMEOUT",
            "error_type": timeout_result.error_type,
        }
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        cleanup = []
        post_terminate_access = []
        for session in sessions:
            cleanup.append(provider.terminate(session).to_dict())
            try:
                provider.get_file(session, REMOTE_MARKER)
            except Exception:
                post_terminate_access.append(False)
            else:
                post_terminate_access.append(True)
        report["cleanup"] = cleanup
        report["checks"]["post_terminate_data_access"] = {
            "passed": bool(post_terminate_access) and not any(post_terminate_access),
            "accessible_after_terminate": post_terminate_access,
        }
        report["finished_at"] = datetime.now(UTC).isoformat()

    cleanup_passed = bool(report["cleanup"]) and all(
        item.get("destroyed") is True for item in report["cleanup"]
    )
    report["checks"]["cleanup"] = {
        "passed": cleanup_passed,
        "destroyed_count": sum(item.get("destroyed") is True for item in report["cleanup"]),
        "total": len(report["cleanup"]),
    }
    report["status"] = (
        "passed"
        if not report.get("error") and all(
            item.get("passed") is True for item in report["checks"].values()
        )
        else "failed"
    )
    record = store.put_json(attempt_id, "isolation-poc-report.json", report)
    print(json.dumps({"report": report, "artifact": record.to_dict()}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
