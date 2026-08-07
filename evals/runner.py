"""运行 Code、销售和 Browser 固定评测，并输出可归档 JSON/Markdown 报告。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from dotenv import load_dotenv

from sandbox_runtime import ArtifactStore, RequestContext
from sandbox_runtime.browser_workflow import PublicBrowserWorkflow
from sandbox_runtime.models import CleanupResult, ExecutionReceipt, utc_now
from sandbox_runtime.ppio import PPIOSandboxConfig, PPIOSandboxProvider
from sandbox_runtime.provider import SandboxProvider, provider_name
from sandbox_runtime.sales_workflow import SalesAnalysisWorkflow
from sandbox_runtime.tencent_agsx import TencentAGSXConfig, TencentAGSXProvider


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE_ROOT = PROJECT_ROOT / "evals" / "cases"
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "artifacts" / "evals"


def load_cases(suite: str) -> list[dict[str, Any]]:
    path = CASE_ROOT / f"{suite}_cases.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"Invalid case file: {path}")
    return payload


def aggregate_results(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records = list(results)
    total = len(records)
    passed = sum(bool(item.get("passed")) for item in records)
    expected_successes = [item for item in records if item.get("expected_success") is True]
    actual_successes = sum(bool(item.get("success")) for item in expected_successes)
    cleanup_checked = [item for item in records if item.get("cleanup_checked")]
    cleanup_passed = sum(bool(item.get("sandbox_destroyed")) for item in cleanup_checked)
    durations = [int(item["duration_ms"]) for item in records if item.get("duration_ms") is not None]
    billing_seconds = sum(
        int(item.get("billable_seconds_estimate", 0)) for item in records
    )
    max_concurrency = _max_observed_concurrency(records)
    costs = [
        float(item["estimated_cost_cny"])
        for item in records
        if item.get("estimated_cost_cny") is not None
    ]
    total_cost = round(sum(costs), 6) if costs and len(costs) == total else None
    return {
        "total_cases": total,
        "passed_cases": passed,
        "failed_cases": total - passed,
        "case_pass_rate": round(passed / total, 4) if total else 0.0,
        "case_pass_rate_ci95": _wilson_interval(passed, total),
        "expected_success_cases": len(expected_successes),
        "actual_successes": actual_successes,
        "task_success_rate": (
            round(actual_successes / len(expected_successes), 4)
            if expected_successes
            else None
        ),
        "task_success_rate_ci95": _wilson_interval(
            actual_successes, len(expected_successes)
        ),
        "cleanup_checked": len(cleanup_checked),
        "cleanup_passed": cleanup_passed,
        "cleanup_rate": (
            round(cleanup_passed / len(cleanup_checked), 4)
            if cleanup_checked
            else None
        ),
        "cleanup_rate_ci95": _wilson_interval(cleanup_passed, len(cleanup_checked)),
        "p50_duration_ms": _percentile(durations, 0.50),
        "p95_duration_ms": _percentile(durations, 0.95),
        "billable_seconds_estimate": billing_seconds,
        "estimated_cost_total_cny": total_cost,
        "estimated_cost_per_successful_task_cny": (
            round(total_cost / actual_successes, 6)
            if total_cost is not None and actual_successes
            else None
        ),
        "observed_max_concurrency": max_concurrency,
    }


def _provider_from_env(provider_name: str, *, require_browser: bool) -> SandboxProvider:
    if provider_name == "tencent-agsx":
        config = TencentAGSXConfig.from_env(
            require_browser=require_browser,
            default_browser_template="lazyx-browser-public-poc",
        )
        return TencentAGSXProvider(config)
    if provider_name == "ppio-sandbox":
        config = PPIOSandboxConfig.from_env(require_browser=require_browser)
        return PPIOSandboxProvider(config)
    raise ValueError(f"Unsupported provider: {provider_name}")


def run_evaluation(
    *,
    suite: str,
    repetitions: int = 1,
    max_cases: int | None = None,
    concurrency: int = 1,
    cpu_cores: float = 2.0,
    memory_gib: float = 4.0,
    cpu_price_cny_per_core_second: float | None = None,
    memory_price_cny_per_gib_second: float | None = None,
    provider_name: str = "tencent-agsx",
    report_root: Path | str = DEFAULT_REPORT_ROOT,
) -> dict[str, Any]:
    if repetitions < 1 or repetitions > 100:
        raise ValueError("repetitions must be between 1 and 100")
    if max_cases is not None and max_cases < 1:
        raise ValueError("max_cases must be positive")
    if concurrency < 1 or concurrency > 20:
        raise ValueError("concurrency must be between 1 and 20")
    if cpu_cores <= 0 or memory_gib <= 0:
        raise ValueError("cpu_cores and memory_gib must be positive")
    prices = (cpu_price_cny_per_core_second, memory_price_cny_per_gib_second)
    if any(price is not None and price < 0 for price in prices):
        raise ValueError("resource prices cannot be negative")
    if any(price is not None for price in prices) and not all(
        price is not None for price in prices
    ):
        raise ValueError("CPU and memory prices must be provided together")

    load_dotenv(PROJECT_ROOT / ".env")
    require_browser = suite in {"browser", "all"}
    provider = _provider_from_env(provider_name, require_browser=require_browser)
    run_id = f"eval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    output_root = Path(report_root).resolve() / run_id
    output_root.mkdir(parents=True, exist_ok=True)

    suites = (
        ["code", "sales", "browser"]
        if suite == "all"
        else ["code", "sales"]
        if suite == "code-sales"
        else [suite]
    )
    jobs: list[tuple[str, dict[str, Any], int]] = []
    for current_suite in suites:
        cases = load_cases(current_suite)
        if max_cases is not None:
            cases = cases[:max_cases]
        for repetition in range(1, repetitions + 1):
            for case in cases:
                jobs.append((current_suite, case, repetition))

    def execute_job(job: tuple[str, dict[str, Any], int]) -> dict[str, Any]:
        current_suite, case, repetition = job
        return _run_job(provider, output_root, current_suite, case, repetition)

    if concurrency == 1:
        results = [execute_job(job) for job in jobs]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = list(executor.map(execute_job, jobs))

    for result in results:
        if cpu_price_cny_per_core_second is None:
            result["estimated_cost_cny"] = None
        else:
            result["estimated_cost_cny"] = round(
                result["billable_seconds_estimate"]
                * (
                    cpu_cores * cpu_price_cny_per_core_second
                    + memory_gib * memory_price_cny_per_gib_second
                ),
                6,
            )

    summary = aggregate_results(results)
    report = {
        "run_id": run_id,
        "provider": provider_name,
        "suite": suite,
        "repetitions": repetitions,
        "concurrency": concurrency,
        "pricing": {
            "cpu_cores": cpu_cores,
            "memory_gib": memory_gib,
            "cpu_price_cny_per_core_second": cpu_price_cny_per_core_second,
            "memory_price_cny_per_gib_second": memory_price_cny_per_gib_second,
            "basis": "wall-clock lifetime seconds estimate; excludes system disk, network and discounts",
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": summary,
        "results": results,
    }
    (output_root / "evaluation-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_root / "evaluation-report.md").write_text(
        _markdown_report(report),
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "report_dir": str(output_root),
        "summary": summary,
    }


def _run_job(
    provider: SandboxProvider,
    output_root: Path,
    suite: str,
    case: dict[str, Any],
    repetition: int,
) -> dict[str, Any]:
    store = ArtifactStore(output_root)
    if suite == "code":
        return _run_code_case(provider, store, case, repetition)
    if suite == "sales":
        return _run_sales_case(provider, store, case, repetition)
    return _run_browser_case(provider, store, case, repetition)


def _run_code_case(
    provider: SandboxProvider,
    store: ArtifactStore,
    case: dict[str, Any],
    repetition: int,
) -> dict[str, Any]:
    started_at = utc_now()
    started = time.perf_counter()
    attempt_id = _attempt_id("eval-code", case["case_id"], repetition)
    context = RequestContext("eval-tenant", "eval-user", "code-eval", attempt_id)
    receipt = ExecutionReceipt(
        receipt_id=f"receipt-{uuid4().hex}",
        context=context,
        provider=provider_name(provider),
        action="eval_code",
        started_at=started_at,
    )
    session = None
    cleanup: CleanupResult | None = None
    success = False
    assertion_passed: bool | None = None
    error_type: str | None = None
    evidence: dict[str, Any] = {}
    try:
        session = provider.create_code(context, ttl_seconds=300)
        receipt.sandbox_id = session.sandbox_id
        kind = case["kind"]
        if kind == "file_round_trip":
            payload = f"eval-payload-{uuid4().hex}".encode()
            remote_path = "/tmp/lazyx/evals/file-round-trip.bin"
            provider.put_file(session, remote_path, payload)
            downloaded = provider.get_file(session, remote_path)
            success = downloaded == payload
            assertion_passed = success == bool(case.get("expected_success"))
            evidence = {
                "size_bytes": len(downloaded),
                "sha256": hashlib.sha256(downloaded).hexdigest(),
            }
        else:
            execution_timeout = 1 if kind == "timeout" else 10
            execution = provider.execute_code(
                session,
                case["code"],
                language="javascript" if kind == "javascript" else "python",
                timeout_seconds=execution_timeout,
            )
            success = execution.success
            error_type = execution.error_type
            assertion_passed = success == bool(case.get("expected_success"))
            evidence = {
                "stdout": execution.stdout[:1000],
                "stderr": execution.stderr[:1000],
                "execution_duration_ms": execution.duration_ms,
            }
            if case.get("expected_stdout"):
                assertion_passed = assertion_passed and (
                    case["expected_stdout"] in execution.stdout
                )
            if case.get("expected_error_type"):
                assertion_passed = assertion_passed and (
                    (not execution.success)
                    and execution.error_type == case["expected_error_type"]
                )
    except Exception as exc:
        error_type = type(exc).__name__
        evidence = {"message": str(exc)[:500]}
        receipt.error = evidence["message"]
    finally:
        if session is not None:
            cleanup = provider.terminate(session)
            receipt.cleanup = cleanup
        receipt.status = "passed" if success and cleanup is not None and cleanup.destroyed else "failed"
        receipt.finished_at = utc_now()
        if receipt.status == "failed" and receipt.error is None:
            receipt.error = error_type or (cleanup.error if cleanup else "code evaluation failed")
    try:
        receipt_record = store.put_json(attempt_id, "execution-receipt.json", receipt.to_dict())
        evidence["receipt"] = receipt_record.to_dict()
    except Exception as exc:
        evidence["receipt_error"] = f"{type(exc).__name__}: {exc}"
    return _record(
        suite="code",
        case=case,
        repetition=repetition,
        attempt_id=attempt_id,
        started=started,
        success=success,
        error_type=error_type,
        cleanup=cleanup,
        evidence=evidence,
        assertion_passed=assertion_passed,
        started_at=started_at,
    )


def _run_sales_case(
    provider: SandboxProvider,
    store: ArtifactStore,
    case: dict[str, Any],
    repetition: int,
) -> dict[str, Any]:
    started_at = utc_now()
    started = time.perf_counter()
    attempt_id = _attempt_id("eval-sales", case["case_id"], repetition)
    context = RequestContext("eval-tenant", "eval-user", "sales-eval", attempt_id)
    try:
        csv_path = Path(case["path"])
        if not csv_path.is_absolute():
            csv_path = PROJECT_ROOT / csv_path
        result = SalesAnalysisWorkflow(provider, store).run(csv_path, context)
        success = result.success
        assertion_passed = success == bool(case.get("expected_success"))
        if case.get("expected_total_sales") is not None:
            assertion_passed = assertion_passed and (
                result.summary is not None
                and result.summary.get("total_sales") == case["expected_total_sales"]
            )
        if case.get("expected_row_count") is not None:
            assertion_passed = assertion_passed and (
                result.summary is not None
                and result.summary.get("row_count") == case["expected_row_count"]
            )
        receipt = json.loads(store.read_bytes(result.receipt))
        cleanup = _cleanup_from_dict(receipt.get("cleanup"))
        return _record(
            suite="sales",
            case=case,
            repetition=repetition,
            attempt_id=attempt_id,
            started=started,
            success=success,
            error_type=None if result.success else "SALES_WORKFLOW_FAILED",
            cleanup=cleanup,
            evidence={
                "summary": result.summary,
                "artifacts": [item.to_dict() for item in result.artifacts],
                "receipt": result.receipt.to_dict(),
            },
            assertion_passed=assertion_passed,
            started_at=started_at,
        )
    except Exception as exc:
        return _record(
            suite="sales",
            case=case,
            repetition=repetition,
            attempt_id=attempt_id,
            started=started,
            success=False,
            error_type=type(exc).__name__,
            cleanup=None,
            evidence={"message": str(exc)[:500]},
            started_at=started_at,
        )


def _run_browser_case(
    provider: SandboxProvider,
    store: ArtifactStore,
    case: dict[str, Any],
    repetition: int,
) -> dict[str, Any]:
    started_at = utc_now()
    started = time.perf_counter()
    attempt_id = _attempt_id("eval-browser", case["case_id"], repetition)
    context = RequestContext("eval-tenant", "eval-user", "browser-eval", attempt_id)
    try:
        result = PublicBrowserWorkflow(provider, store).run(case["url"], context)
        success = result.success
        assertion_passed = success == bool(case.get("expected_success"))
        if case.get("expected_status_code") is not None:
            assertion_passed = assertion_passed and (
                result.status_code == case["expected_status_code"]
            )
        if case.get("expected_title"):
            assertion_passed = assertion_passed and (
                result.title == case["expected_title"]
            )
        return _record(
            suite="browser",
            case=case,
            repetition=repetition,
            attempt_id=attempt_id,
            started=started,
            success=success,
            error_type=None if result.success else "BROWSER_WORKFLOW_FAILED",
            cleanup=result.cleanup,
            evidence={
                "status_code": result.status_code,
                "title": result.title,
                "final_url": result.final_url,
                "artifacts": [item.to_dict() for item in result.artifacts],
                "receipt": result.receipt.to_dict(),
            },
            assertion_passed=assertion_passed,
            started_at=started_at,
        )
    except Exception as exc:
        return _record(
            suite="browser",
            case=case,
            repetition=repetition,
            attempt_id=attempt_id,
            started=started,
            success=False,
            error_type=type(exc).__name__,
            cleanup=None,
            evidence={"message": str(exc)[:500]},
            started_at=started_at,
        )


def _record(
    *,
    suite: str,
    case: dict[str, Any],
    repetition: int,
    attempt_id: str,
    started: float,
    success: bool,
    error_type: str | None,
    cleanup: CleanupResult | None,
    evidence: dict[str, Any],
    assertion_passed: bool | None = None,
    started_at=None,
) -> dict[str, Any]:
    duration_ms = round((time.perf_counter() - started) * 1000)
    finished_at = utc_now()
    expected_success = bool(case.get("expected_success"))
    passed = assertion_passed if assertion_passed is not None else success == expected_success
    return {
        "suite": suite,
        "case_id": case["case_id"],
        "repetition": repetition,
        "attempt_id": attempt_id,
        "expected_success": expected_success,
        "success": success,
        "passed": passed,
        "duration_ms": duration_ms,
        "started_at": started_at.isoformat() if started_at else None,
        "finished_at": finished_at.isoformat(),
        "error_type": error_type,
        "cleanup_checked": cleanup is not None,
        "sandbox_destroyed": cleanup.destroyed if cleanup else False,
        "cleanup_error": cleanup.error if cleanup else None,
        "billable_seconds_estimate": math.ceil(duration_ms / 1000)
        if cleanup is not None
        else 0,
        "estimated_cost_cny": None,
        "evidence": evidence,
    }


def _cleanup_from_dict(value: Any) -> CleanupResult | None:
    if not isinstance(value, dict) or not value.get("sandbox_id"):
        return None
    return CleanupResult(
        str(value["sandbox_id"]),
        bool(value.get("destroyed")),
        value.get("error"),
    )


def _attempt_id(prefix: str, case_id: str, repetition: int) -> str:
    return f"{prefix}-{case_id}-r{repetition}-{uuid4().hex[:8]}"


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.9999)))
    return ordered[index]


def _wilson_interval(successes: int, total: int) -> list[float] | None:
    """返回二项比例的 95% Wilson 区间，避免小样本使用正态区间越界。"""
    if total <= 0:
        return None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
        / denominator
    )
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def _max_observed_concurrency(records: list[dict[str, Any]]) -> int:
    events: list[tuple[str, int]] = []
    for record in records:
        if not record.get("started_at") or not record.get("finished_at"):
            continue
        events.append((record["started_at"], 1))
        events.append((record["finished_at"], -1))
    active = maximum = 0
    for _timestamp, delta in sorted(events, key=lambda item: (item[0], -item[1])):
        active += delta
        maximum = max(maximum, active)
    return maximum


def _markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"# Sandbox Evaluation {report['run_id']}",
        "",
        f"- Suite: `{report['suite']}`",
        f"- Provider: `{report['provider']}`",
        f"- Generated: `{report['generated_at']}`",
        f"- Case pass rate: `{summary['case_pass_rate']}`",
        f"- Case pass rate 95% CI: `{summary['case_pass_rate_ci95']}`",
        f"- Cleanup rate: `{summary['cleanup_rate']}`",
        f"- Cleanup rate 95% CI: `{summary['cleanup_rate_ci95']}`",
        f"- P50/P95 duration: `{summary['p50_duration_ms']}` / `{summary['p95_duration_ms']}` ms",
        f"- Billable seconds estimate: `{summary['billable_seconds_estimate']}`",
        f"- Estimated cost per successful task: `{summary['estimated_cost_per_successful_task_cny']}` CNY",
        f"- Observed max concurrency: `{summary['observed_max_concurrency']}`",
        "",
        "| Suite | Case | Repetition | Passed | Success | Duration ms | Destroyed | Error |",
        "|---|---|---:|---|---|---:|---|---|",
    ]
    for item in report["results"]:
        lines.append(
            f"| {item['suite']} | {item['case_id']} | {item['repetition']} | "
            f"{item['passed']} | {item['success']} | {item['duration_ms']} | "
            f"{item['sandbox_destroyed']} | {item['error_type'] or ''} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fixed sandbox evaluations.")
    parser.add_argument(
        "--suite",
        choices=("code", "sales", "browser", "code-sales", "all"),
        default="all",
    )
    parser.add_argument("--provider", choices=("tencent-agsx", "ppio-sandbox"), default="tencent-agsx")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--cpu-cores", type=float, default=2.0)
    parser.add_argument("--memory-gib", type=float, default=4.0)
    parser.add_argument("--cpu-price-cny-per-core-second", type=float, default=None)
    parser.add_argument("--memory-price-cny-per-gib-second", type=float, default=None)
    parser.add_argument("--report-root", default=str(DEFAULT_REPORT_ROOT))
    args = parser.parse_args()
    try:
        result = run_evaluation(
            suite=args.suite,
            repetitions=args.repetitions,
            max_cases=args.max_cases,
            concurrency=args.concurrency,
            cpu_cores=args.cpu_cores,
            memory_gib=args.memory_gib,
            cpu_price_cny_per_core_second=args.cpu_price_cny_per_core_second,
            memory_price_cny_per_gib_second=args.memory_price_cny_per_gib_second,
            provider_name=args.provider,
            report_root=args.report_root,
        )
    except (RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "error_type": "CONFIGURATION_ERROR",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["summary"]["failed_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
