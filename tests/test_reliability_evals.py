"""离线基线的证据完整性、已复现缺陷和指标分母回归。"""

from copy import deepcopy
import json
import socket

import pytest

from evals import reliability_runner as runner


@pytest.fixture(scope="module")
def evaluated(tmp_path_factory):
    output = tmp_path_factory.mktemp("reliability") / "run"
    report = runner.run_suite(output)
    return output, report


def test_baseline_preserves_real_failures_and_all_suites(evaluated):
    output, report = evaluated
    assert len(runner.load_dataset().cases) == 50
    assert report["summary"]["planned"] == 50
    assert report["summary"]["passed"] == 49
    assert {r["case_id"] for r in report["cases"] if not r["passed"]} == {"RT-E10"}
    assert next(row for row in report["cases"] if row["case_id"] == "RT-E08")["passed"]
    assert next(row for row in report["cases"] if row["case_id"] == "RT-E09")["passed"]
    # 这里断言框架保留已知失败；这些场景在 Benchmark 中仍然失败。
    assert not any(row["status"] == "harness_error" for row in report["cases"])
    assert (output / "environments/RT-E07-1/chat.sqlite3").is_file()
    assert (output / "environments/RT-E07-1/effects.jsonl").is_file()
    assert json.loads((output / "report.json").read_text())["summary"]["passed"] == 49


@pytest.mark.parametrize("case_id", ["RT-E01", "RT-E02", "RT-E03", "RT-E04", "RT-E05", "RT-E06", "RT-E07", "RT-E11"])
def test_scorer_rejects_deleted_required_evidence(evaluated, case_id):
    _, report = evaluated
    case = next(c for c in runner.load_dataset().cases if c.case_id == case_id)
    row = deepcopy(next(r for r in report["cases"] if r["case_id"] == case_id))
    assert runner.evaluate(case, row)[0]
    del row["evidence"]
    row["passed"] = True
    assert not runner.evaluate(case, row)[0]


@pytest.mark.parametrize("corruption", ["jobs", "effects", "wrong_execution", "unverified_readback", "crash_evidence"])
def test_cannot_forge_success_by_changing_checks(evaluated, corruption):
    _, report = evaluated
    case = next(c for c in runner.load_dataset().cases if c.case_id == "RT-E07")
    row = deepcopy(next(r for r in report["cases"] if r["case_id"] == case.case_id))
    if corruption in {"jobs", "effects"}:
        row["evidence"][corruption] = []
    elif corruption == "wrong_execution":
        row["evidence"]["jobs"][0]["execution_id"] = "unrelated"
    elif corruption == "unverified_readback":
        row["evidence"]["jobs"][0]["result"]["verification"]["fields"]["body"]["matched"] = False
    else:
        row["at_crash"] = {}
    row["checks"] = {key: True for key in row["checks"]}
    row["passed"] = True
    assert not runner.evaluate(case, row)[0]


def test_stale_receipt_failure_does_not_become_duplicate_side_effect(evaluated):
    _, report = evaluated
    metrics = report["summary"]["metrics"]
    assert metrics["idempotency_success_rate"] == runner.fraction(8, 8)
    assert metrics["duplicate_side_effect_rate"] == runner.fraction(0, 10)
    assert metrics["false_completion_rate"] == runner.fraction(0, 3)
    assert metrics["live_model_task_success_rate"] is None
    assert metrics["tokens_per_successful_task"] is None


def test_unrun_remains_in_denominator_and_unknown_is_not_safe_zero():
    case = next(c for c in runner.load_dataset().cases if c.case_id == "RT-E08")
    row = {"case_id": case.case_id, "repetition": 1, "status": "not_run"}
    summary = runner.summarize([case], [row], 1)
    assert summary["planned"] == 1 and summary["passed"] == 0
    assert summary["metrics"]["state_consistency_rate"]["denominator"] == 1
    assert summary["metrics"]["duplicate_side_effect_rate"] == runner.fraction(0, 1, 1)
    assert summary["metrics"]["false_completion_rate"] == runner.fraction(0, 1, 1)


def test_reused_reports_cannot_be_substituted_with_other_case(evaluated):
    _, report = evaluated
    case = next(c for c in runner.load_dataset().cases if c.case_id == "CO-N01")
    other = next(r for r in report["cases"] if r["case_id"] == "CO-N03")
    assert not runner.evaluate(case, other)[0]


def test_all_k_is_distinct_from_any_k(evaluated):
    _, report = evaluated
    case = next(c for c in runner.load_dataset().cases if c.case_id == "RT-E01")
    success = deepcopy(next(r for r in report["cases"] if r["case_id"] == case.case_id))
    failure = {"case_id": case.case_id, "repetition": 2, "status": "not_run"}
    summary = runner.summarize([case], [success, failure], 2)
    assert summary["repeated_runs"]["all_k_success"]["value"] == 0
    assert summary["repeated_runs"]["any_k_success"]["value"] == 1
    assert summary["repeated_runs"]["single_run"]["value"] == .5


def test_isolation_blocks_network_and_restores_paths(tmp_path):
    original = runner.rpa_xiaohongshu.RPA_XHS_DATA_DIR
    with runner.isolated(tmp_path):
        assert runner.rpa_xiaohongshu.RPA_XHS_DATA_DIR == tmp_path / "rpa"
        with socket.socket() as sock, pytest.raises(AssertionError, match="禁止网络"):
            sock.connect(("127.0.0.1", 9))
    assert runner.rpa_xiaohongshu.RPA_XHS_DATA_DIR == original


def test_interruption_archives_unrun_cases(tmp_path, monkeypatch):
    def interrupt(case, root):
        raise KeyboardInterrupt
    monkeypatch.setattr(runner, "run_probe", interrupt)
    output = tmp_path / "interrupted"
    report = runner.run_suite(output, ["RT-E01", "RT-E02"])
    assert [r["status"] for r in report["cases"]] == ["interrupted", "not_run"]
    assert report["summary"]["planned"] == 2 and report["summary"]["passed"] == 0
    assert (output / "report.md").is_file()


def test_selection_does_not_create_output_on_error(tmp_path):
    output = tmp_path / "invalid"
    with pytest.raises(ValueError, match="无效"):
        runner.run_suite(output, ["NO_SUCH_CASE"])
    assert not output.exists()
