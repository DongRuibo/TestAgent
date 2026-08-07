"""恢复 Gateway 本地未完成记录，并扫描带项目标签的远端孤儿实例。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sandbox_gateway.factory import build_service


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recover unfinished Gateway executions and clean managed orphans."
    )
    parser.add_argument("--max-age-seconds", type=int, default=600)
    parser.add_argument(
        "--skip-orphan-scan",
        action="store_true",
        help="Only mark local in-flight executions as failed; do not call Provider list/kill APIs.",
    )
    args = parser.parse_args()
    if args.max_age_seconds < 60:
        raise SystemExit("--max-age-seconds must be at least 60")

    service = build_service()
    try:
        report = {
            "started_at": datetime.now(UTC).isoformat(),
            "recovered_execution_ids": service.recover_after_restart(),
            "orphan_cleanup": (
                {"status": "skipped"}
                if args.skip_orphan_scan
                else service.cleanup_orphans(max_age_seconds=args.max_age_seconds)
            ),
        }
        report["finished_at"] = datetime.now(UTC).isoformat()
        output_dir = Path(
            service.artifact_store.root
        ) / "recovery"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"recovery-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"report={output}")
        return 0
    finally:
        service.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
