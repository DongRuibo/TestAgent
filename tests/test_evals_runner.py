"""评测用例加载和指标聚合的离线测试。"""

from evals.runner import aggregate_results, load_cases


def test_load_cases_contains_fixed_suites() -> None:
    assert load_cases("code")[0]["case_id"] == "python_basic"
    assert load_cases("sales")[0]["expected_total_sales"] == 800000.0
    assert load_cases("browser")[0]["url"].startswith("https://example.com/")
    assert len(load_cases("browser")) == 30
    assert len(load_cases("code")) == 30


def test_aggregate_results_distinguishes_expected_failure_and_cleanup() -> None:
    summary = aggregate_results(
        [
            {
                "passed": True,
                "expected_success": True,
                "success": True,
                "cleanup_checked": True,
                "sandbox_destroyed": True,
                "duration_ms": 100,
                "billable_seconds_estimate": 1,
                "estimated_cost_cny": 0.001,
            },
            {
                "passed": True,
                "expected_success": False,
                "success": False,
                "cleanup_checked": True,
                "sandbox_destroyed": True,
                "duration_ms": 300,
                "billable_seconds_estimate": 1,
                "estimated_cost_cny": 0.001,
            },
            {
                "passed": False,
                "expected_success": True,
                "success": False,
                "cleanup_checked": False,
                "sandbox_destroyed": False,
                "duration_ms": 200,
                "billable_seconds_estimate": 0,
                "estimated_cost_cny": 0.0,
            },
        ]
    )

    assert summary["total_cases"] == 3
    assert summary["passed_cases"] == 2
    assert summary["case_pass_rate"] == 0.6667
    assert summary["case_pass_rate_ci95"][0] >= 0.0
    assert summary["case_pass_rate_ci95"][1] <= 1.0
    assert summary["expected_success_cases"] == 2
    assert summary["actual_successes"] == 1
    assert summary["task_success_rate"] == 0.5
    assert summary["cleanup_rate"] == 1.0
    assert summary["cleanup_rate_ci95"][1] <= 1.0
    assert summary["p50_duration_ms"] == 200
    assert summary["p95_duration_ms"] == 300
    assert summary["billable_seconds_estimate"] == 2
    assert summary["estimated_cost_total_cny"] == 0.002
    assert summary["estimated_cost_per_successful_task_cny"] == 0.002
