"""v2只更正RT-E10的已确认语义；同一运行同时保留v1判分，绝不覆盖历史。"""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from evals import reliability_runner as v1


DATASET = Path(__file__).parent / "datasets/reliability_v2.json"


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["2.0.0"]
    cases: list[v1.Case]


def load_dataset():
    dataset = Dataset.model_validate_json(DATASET.read_text("utf-8"))
    if len({c.case_id for c in dataset.cases}) != len(dataset.cases):
        raise ValueError("用例编号重复")
    return dataset


def evaluate(case, row):
    ok, reasons = v1.evaluate(case, row)
    if case.case_id != "RT-E10":
        return ok, reasons
    evidence = row.get("evidence", {})
    response = row.get("replayed_response", {})
    drafts = evidence.get("drafts", [])
    originals = [json.loads(a["response"]) for a in evidence.get("actions", []) if a["action"] == "save_content_draft"]
    checks = {
        "one_original_receipt": len(originals) == 1,
        "explicit_replay": response.get("replayed") is True,
        "historical_fields_unchanged": len(originals) == 1 and all(response.get(k) == value for k, value in originals[0].items()),
        "original_status_traceable": len(originals) == 1 and response.get("original_execution_status") == originals[0]["status"],
        "current_state_readback": bool(drafts) and response.get("current_business_status") == drafts[-1]["execution_status"],
        "current_target_snapshot": bool(drafts) and response.get("current_data_patch") == drafts[-1],
    }
    return ok and all(checks.values()), reasons + [key for key, value in checks.items() if not value]


def rescore(report):
    """基础统计复用v1；仅重算依赖新版场景契约的汇总项，风险观测与耗时不变。"""
    cases = load_dataset().cases
    rows = deepcopy(report["cases"])
    contracts = {c.case_id: c for c in cases}
    expected = {(c.case_id, rep) for c in cases for rep in range(1, report["summary"]["repeated_runs"]["k"] + 1)}
    if len(rows) != len(expected) or {(r["case_id"], r["repetition"]) for r in rows} != expected:
        raise ValueError("需要完整且无重复的50例固定运行证据")
    k = report["summary"]["repeated_runs"]["k"]
    summary = v1.summarize(cases, rows, k)
    for row in rows:
        row["passed"], row["failure_reasons"] = evaluate(contracts[row["case_id"]], row)
    summary["passed"] = sum(r["passed"] for r in rows)
    for tag, name in [("goal", "task_success_rate"), ("state", "state_consistency_rate"), ("recovery", "recovery_success_rate")]:
        selected = [r for r in rows if tag in contracts[r["case_id"]].tags]
        # v2唯一修改的RT-E10没有这些指标的局部metric_checks。
        summary["metrics"][name] = v1.fraction(sum(r["passed"] for r in selected), len(selected))
    summary["repeated_runs"].update(single_run=v1.fraction(summary["passed"], len(rows)),
        all_k_success=v1.fraction(sum(all(r["passed"] for r in rows if r["case_id"] == c.case_id) for c in cases), len(cases)),
        any_k_success=v1.fraction(sum(any(r["passed"] for r in rows if r["case_id"] == c.case_id) for c in cases), len(cases)))
    for suite in summary["by_suite"]:
        selected = [r for r in rows if contracts[r["case_id"]].suite == suite]
        summary["by_suite"][suite] = v1.fraction(sum(r["passed"] for r in selected), len(selected))
    summary["failures"] = [{"case_id": r["case_id"], "repetition": r["repetition"], "layer": r.get("failure_layer"),
        "status": r["status"], "reasons": r["failure_reasons"]} for r in rows if not r["passed"]]
    return {**deepcopy(report), "dataset_version": "2.0.0", "summary": summary, "cases": rows,
        "contract_change": "Only RT-E10: immutable historical receipt plus same-version current snapshot; v1 scores retained separately"}


def run_suite(output, repetitions=3, input_report=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    source = Path(input_report) if input_report else output / "v1-observations/report.json"
    if input_report is None:
        v1.run_suite(output / "v1-observations", repetitions=repetitions)
    original = json.loads(source.read_text("utf-8"))
    report = rescore(original)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    manifest = {"dataset_version": "2.0.0", "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        "v1_dataset_sha256": hashlib.sha256(v1.DATASET.read_bytes()).hexdigest(),
        "v1_scorer_sha256": hashlib.sha256(Path(v1.__file__).read_bytes()).hexdigest(),
        "v2_scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "input_report_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "paired_v1_summary": original["summary"],
        "scope": "same observations; versioned contract; not a new model experiment"}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    lines = ["# Reliability v2", "", f"场景契约：{report['summary']['passed']}/{report['summary']['planned']}。", "",
        "仅RT-E10契约升级，原始v1判分独立保留；不能把跨契约差值作为代码优化收益。", "",
        "| Case | 次数 | 通过 | 失败原因 |", "|---|---:|---|---|"]
    lines += [f"| {r['case_id']} | {r['repetition']} | {r['passed']} | {', '.join(r['failure_reasons'])} |" for r in report["cases"]]
    (output / "report.md").write_text("\n".join(lines) + "\n", "utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--input-report", type=Path)
    options = parser.parse_args()
    report = run_suite(options.output_dir, options.repetitions, options.input_report)
    print(json.dumps(report["summary"], ensure_ascii=False))
    raise SystemExit(0 if report["summary"]["passed"] == report["summary"]["planned"] else 1)
