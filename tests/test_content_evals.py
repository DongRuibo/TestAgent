"""业务评测实际驱动生产逻辑，并验证评分器拒绝不完整和被篡改的成功证据。"""

from copy import deepcopy
import json

import pytest

from evals import content_runner


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    return content_runner.run_suite(tmp_path_factory.mktemp("content-eval") / "run")


@pytest.mark.parametrize("case_id", [case.id for case in content_runner.load_suite().cases])
def test_business_case_uses_actual_state_and_matches_contract(report, case_id):
    case = next(case for case in report["cases"] if case["case_id"] == case_id)
    assert case["passed"], case
    assert content_runner.score_observation(case)
    assert case["usage"] is None and case["cost"] is None and case["content_quality"] is None


@pytest.mark.parametrize("corruption", ["missing_job", "unverified_job", "wrong_execution", "wrong_actual", "empty_checks", "harness_error"])
def test_scorer_rejects_plausible_success_with_missing_or_wrong_evidence(report, corruption):
    case = deepcopy(next(case for case in report["cases"] if case["case_id"] == "N02"))
    if corruption == "missing_job": case["jobs"] = []
    elif corruption == "unverified_job": case["jobs"][0]["verified"] = False
    elif corruption == "wrong_execution": case["jobs"][0]["execution_id"] = "another-execution"
    elif corruption == "wrong_actual": case["checks"][0]["actual"] = "not-the-expected-value"
    elif corruption == "empty_checks": case["checks"] = []
    else: case["status"] = "harness_error"
    assert not content_runner.score_observation(case)


@pytest.mark.parametrize("case_id", ["N01", "N02", "N03", "N04", "N05", "N06", "I06", "A01", "A02", "A03", "A04",
    "R01", "R02", "R03", "R04", "R05", "R06", "P01"])
def test_scorer_rejects_deleted_required_drafts_for_each_applicable_scenario(report, case_id):
    case = deepcopy(next(case for case in report["cases"] if case["case_id"] == case_id))
    case["drafts"] = []
    assert not content_runner.score_observation(case)


@pytest.mark.parametrize("case_id", [case.id for case in content_runner.load_suite().cases])
@pytest.mark.parametrize("corruption", ["delete_required_check", "forge_actual_and_expected"])
def test_scorer_uses_fixed_scenario_truth_instead_of_self_reported_checks(report, case_id, corruption):
    case = deepcopy(next(case for case in report["cases"] if case["case_id"] == case_id))
    if corruption == "delete_required_check":
        case["checks"].pop(0)
    else:
        case["checks"][0].update(actual="forged-value", expected="forged-value", passed=True)
    assert not content_runner.score_observation(case)


@pytest.mark.parametrize("case_id,evidence", [
    ("N03", "history"), ("N04", "second_content"), ("A02", "old_approval"), ("A03", "history"),
    ("N02", "executions"), ("R01", "executions"), ("R02", "jobs"), ("R03", "executions"),
    ("R04", "jobs"), ("R05", "executions"), ("R06", "jobs"), ("N05", "generation_requests"),
])
def test_scorer_requires_version_approval_and_execution_evidence(report, case_id, evidence):
    case = deepcopy(next(case for case in report["cases"] if case["case_id"] == case_id))
    if evidence == "history": case["drafts"] = [case["drafts"][-1]]
    elif evidence == "second_content":
        content_id = case["drafts"][0]["content_id"]
        case["drafts"] = [draft for draft in case["drafts"] if draft["content_id"] == content_id]
    elif evidence == "old_approval":
        for draft in case["drafts"]: draft["approval_version"] = None
    else: case[evidence] = []
    assert not content_runner.score_observation(case)


def test_summary_keeps_unrun_cases_and_separates_normal_from_refusals(report):
    results = deepcopy(report["cases"])
    first = next(case for case in results if case["case_id"] == "N01")
    first.update(status="not_run", checks=[], events=[])
    summary = content_runner.summarize(results)
    assert summary["planned"] == 24 and summary["passed"] == 23
    assert summary["normal_flow_pass_rate"] == 5 / 6
    assert summary["expected_failure_pass_rate"] == 1
    assert summary["task_success_rate"] is None and summary["cost"] is None


def test_interrupt_preserves_pending_cases_and_archives_report(tmp_path, monkeypatch):
    def interrupted(case):
        return {"case_id": case.id, "kind": case.kind, "scenario": case.scenario, "mode": "scripted",
            "status": "interrupted", "checks": [], "events": [], "passed": False}
    monkeypatch.setattr(content_runner, "run_case", interrupted)
    output = tmp_path / "interrupted"
    report = content_runner.run_suite(output, cases=["N01", "N02"])
    assert [item["status"] for item in report["cases"]] == ["interrupted", "not_run"]
    assert report["summary"]["planned"] == 2 and report["summary"]["passed"] == 0
    assert json.loads((output / "manifest.json").read_text())["mode"] == "scripted"
    assert (output / "events.jsonl").is_file() and (output / "report.md").is_file()


def test_selection_is_validated_before_creating_output(tmp_path):
    output = tmp_path / "missing"
    with pytest.raises(ValueError, match="编号不存在"):
        content_runner.run_suite(output, cases=["BAD"])
    assert not output.exists()
