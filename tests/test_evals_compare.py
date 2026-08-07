"""Provider 评测报告比较器的离线测试。"""

from __future__ import annotations

import json

from evals.compare import compare_reports


def _write_report(path, provider: str, suite: str = "code") -> None:
    path.write_text(
        json.dumps(
            {
                "provider": provider,
                "suite": suite,
                "repetitions": 1,
                "concurrency": 3,
                "summary": {
                    "case_pass_rate": 1.0,
                    "task_success_rate": 1.0,
                    "cleanup_rate": 1.0,
                    "p50_duration_ms": 100,
                    "p95_duration_ms": 200,
                    "estimated_cost_per_successful_task_cny": 0.001,
                },
            }
        ),
        encoding="utf-8",
    )


def test_compare_reports_calculates_directional_delta(tmp_path) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    _write_report(left, "tencent-agsx")
    _write_report(right, "ppio-sandbox")
    payload = compare_reports(left, right)

    p95 = next(item for item in payload["metrics"] if item["metric"] == "p95_duration_ms")
    assert payload["left"]["provider"] == "tencent-agsx"
    assert payload["right"]["provider"] == "ppio-sandbox"
    assert p95["right_minus_left"] == 0
