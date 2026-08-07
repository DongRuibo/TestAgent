"""审计评测报告中的逐实例 Receipt、文件哈希和清理状态。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def audit(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    root = path.parent
    errors: list[str] = []
    checked = 0
    for item in report.get("results", []):
        suite = item.get("suite", "unknown")
        case_id = item.get("case_id", "unknown")
        receipt = item.get("evidence", {}).get("receipt")
        if not isinstance(receipt, dict):
            errors.append(f"{suite}/{case_id}: missing evidence.receipt")
            continue
        checked += 1
        relative_path = receipt.get("relative_path")
        if not isinstance(relative_path, str):
            errors.append(f"{suite}/{case_id}: receipt.relative_path missing")
            continue
        receipt_path = root / relative_path
        if not receipt_path.is_file():
            errors.append(f"{suite}/{case_id}: receipt file missing: {relative_path}")
            continue
        digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        if digest != receipt.get("sha256"):
            errors.append(f"{suite}/{case_id}: receipt SHA256 mismatch")
        receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        cleanup = receipt_payload.get("cleanup")
        if item.get("cleanup_checked") and not isinstance(cleanup, dict):
            errors.append(f"{suite}/{case_id}: cleanup missing in receipt")
        if item.get("cleanup_checked") and bool(item.get("sandbox_destroyed")) != bool(
            cleanup and cleanup.get("destroyed")
        ):
            errors.append(f"{suite}/{case_id}: cleanup result mismatch")
        if receipt_payload.get("sandbox_id") and item.get("cleanup_checked"):
            if cleanup and cleanup.get("sandbox_id") != receipt_payload.get("sandbox_id"):
                errors.append(f"{suite}/{case_id}: sandbox ID mismatch in cleanup")
    return {
        "report": str(path),
        "total_results": len(report.get("results", [])),
        "receipts_checked": checked,
        "errors": errors,
        "passed": not errors and checked == len(report.get("results", [])),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"passed": False, "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
