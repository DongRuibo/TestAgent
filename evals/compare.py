"""比较两个固定评测报告，避免手工抄录 Provider 指标。"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


METRICS = (
    "case_pass_rate",
    "task_success_rate",
    "cleanup_rate",
    "p50_duration_ms",
    "p95_duration_ms",
    "estimated_cost_per_successful_task_cny",
)


def load_report(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("summary"), dict):
        raise ValueError(f"invalid evaluation report: {path}")
    return payload


def compare_reports(left_path: Path, right_path: Path) -> dict:
    left = load_report(left_path)
    right = load_report(right_path)
    if left.get("suite") != right.get("suite"):
        raise ValueError("reports must use the same suite")
    if left.get("repetitions") != right.get("repetitions"):
        raise ValueError("reports must use the same repetitions")
    if left.get("concurrency") != right.get("concurrency"):
        raise ValueError("reports must use the same concurrency")
    rows = []
    for metric in METRICS:
        left_value = left["summary"].get(metric)
        right_value = right["summary"].get(metric)
        delta = (
            round(right_value - left_value, 6)
            if isinstance(left_value, (int, float))
            and isinstance(right_value, (int, float))
            else None
        )
        rows.append(
            {
                "metric": metric,
                "left": left_value,
                "right": right_value,
                "right_minus_left": delta,
            }
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "suite": left["suite"],
        "left": {
            "provider": left.get("provider", "unknown"),
            "report": str(left_path),
        },
        "right": {
            "provider": right.get("provider", "unknown"),
            "report": str(right_path),
        },
        "metrics": rows,
        "limitations": [
            "差异是两份固定评测报告的描述性比较，不是统计显著性检验。",
            "成本只有在两边资源规格、价格口径和账单项一致时才可直接比较。",
        ],
    }


def markdown(payload: dict) -> str:
    lines = [
        f"# Sandbox Provider Comparison ({payload['suite']})",
        "",
        f"- Left: `{payload['left']['provider']}` — `{payload['left']['report']}`",
        f"- Right: `{payload['right']['provider']}` — `{payload['right']['report']}`",
        "",
        "| Metric | Left | Right | Right - Left |",
        "|---|---:|---:|---:|",
    ]
    for row in payload["metrics"]:
        lines.append(
            f"| `{row['metric']}` | {row['left']} | {row['right']} | {row['right_minus_left']} |"
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in payload["limitations"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two sandbox evaluation reports.")
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/evals"))
    args = parser.parse_args()
    try:
        payload = compare_reports(args.left, args.right)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error_type": "INVALID_REPORT", "message": str(exc)}, ensure_ascii=False))
        return 2
    output_dir = args.output_dir / f"comparison-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "comparison.md").write_text(markdown(payload), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
