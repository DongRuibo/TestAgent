"""清理带 lazyx-testagent metadata 的过期 AGSX 实例。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean expired managed AGSX sandboxes.")
    parser.add_argument("--max-age-seconds", type=int, default=600)
    args = parser.parse_args()
    if args.max_age_seconds < 60:
        raise SystemExit("--max-age-seconds must be at least 60")

    load_dotenv()
    provider = TencentAGSXProvider(TencentAGSXConfig.from_env())
    results = provider.cleanup_expired(args.max_age_seconds)
    print(json.dumps([item.to_dict() for item in results], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
