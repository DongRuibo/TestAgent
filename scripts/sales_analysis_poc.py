"""第二周业务闭环：销售 CSV → AGSX → JSON/SVG Artifact → Receipt。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sandbox_runtime import ArtifactStore, RequestContext
from sandbox_runtime.sales_workflow import SalesAnalysisWorkflow
from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AGSX sales analysis POC.")
    parser.add_argument("csv", nargs="?", default="fixtures/sales.csv")
    parser.add_argument("--artifacts", default="artifacts")
    args = parser.parse_args()

    load_dotenv()
    provider = TencentAGSXProvider(TencentAGSXConfig.from_env())
    store = ArtifactStore(Path(args.artifacts))
    attempt_id = f"sales-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    context = RequestContext(
        tenant_id="poc-tenant",
        user_id="boss-agent-user",
        task_id="sales-analysis",
        attempt_id=attempt_id,
    )
    result = SalesAnalysisWorkflow(provider, store).run(Path(args.csv), context)
    print(
        json.dumps(
            {
                "success": result.success,
                "attempt_id": attempt_id,
                "summary": result.summary,
                "artifacts": [item.to_dict() for item in result.artifacts],
                "receipt": result.receipt.to_dict(),
                "error": result.error,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
