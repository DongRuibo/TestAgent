"""验证统计口径与真实证据追溯，不新增或重跑业务场景。"""

import json
import socket
import sqlite3
from unittest.mock import patch

import pytest

from evals import business_execution_metrics as metrics


@pytest.fixture(scope="module")
def bundle():
    from evals import reliability_runner as v1, reliability_v2_runner as v2, content_live_runner

    catalog = metrics.load_catalog()
    # 若误接运行器或联网，这次真实证据分析必须直接失败。
    with patch.object(socket.socket, "connect", side_effect=AssertionError("禁止网络")), \
         patch.object(v1, "run_suite", side_effect=AssertionError("禁止运行场景")), \
         patch.object(v2, "run_suite", side_effect=AssertionError("禁止运行场景")), \
         patch.object(content_live_runner, "run_evaluation", side_effect=AssertionError("禁止调用模型")):
        return metrics.analyze(catalog)


def test_original_failures_and_same_run_two_contracts_are_preserved(bundle):
    sources = bundle["metrics"]["sources"]
    assert sources["baseline"]["v1_summary"]["passed"] == 138
    assert sources["phase3_before"]["v1_summary"]["passed"] == 144
    assert sources["phase3_before"]["v2_summary"]["passed"] == 147
    assert sources["phase3_after"]["v1_summary"]["passed"] == 147
    assert sources["phase3_after"]["v2_summary"]["passed"] == 150
    for source in sources.values():
        if source["kind"] == "reliability":
            assert source["v1_summary_matches_original"] is True
            assert source["task_observations"]["tasks"] == 150
    assert len(sources["phase3_after"]["v1_summary"]["failures"]) == 3
    assert {r["case_id"] for r in sources["phase3_after"]["v1_summary"]["failures"]} == {"RT-E10"}


def test_recovery_contract_is_not_unattended_automation(bundle):
    source = bundle["metrics"]["sources"]["phase3_after"]
    recovery = source["recovery"]
    assert recovery["metrics"]["automatic_business_recovery_rate"] == metrics.fraction(21, 45)
    assert recovery["metrics"]["safe_human_handoff_rate"] == metrics.fraction(12, 45)
    human = [r for r in recovery["case_runs"] if r["category"] == "safe_human_handoff"]
    assert sum(r["execution_states"] == ["needs_review"] for r in human) == 6
    assert sum(r["execution_states"] == ["manually_confirmed"] for r in human) == 3
    assert sum(r["execution_states"] == ["reviewed_not_saved"] for r in human) == 3
    assert source["task_observations"]["automatic_completion_rate"]["value"] is None
    assert source["task_observations"]["abnormal_human_rescue_rate"]["value"] is None


def test_real_model_original_and_retest_keep_failures_and_denominators(bundle):
    sources = bundle["metrics"]["sources"]
    assert sources["live_original"]["normal_mechanical_completion_rate"] == metrics.fraction(0, 4)
    assert sources["live_original"]["task_observations"]["terminal_counts"] == {"failed": 5, "not_run": 3}
    assert sources["live_retest"]["normal_mechanical_completion_rate"] == metrics.fraction(3, 4)
    assert sources["live_retest"]["correct_refusal_rate"] == metrics.fraction(2, 4)
    assert sources["live_retest"]["scenario_contract_rate"] == metrics.fraction(5, 8)
    assert sources["live_original"]["rescored_checks_match_original"]
    assert sources["live_retest"]["rescored_checks_match_original"]
    assert all(r["platform"] == "forbidden_substitute" for r in bundle["tasks"] if r["model"] == "real")
    assert bundle["metrics"]["real_platform"]["verified_save_rate"] == metrics.fraction(0, 0)


def test_robot_all_attempts_do_not_disappear_from_scenario_coverage(bundle):
    robot = bundle["metrics"]["sources"]["robot_loopback"]
    assert robot["canonical"]["jobs"] == 11
    assert robot["canonical"]["status_counts"] == {"needs_review": 5, "succeeded": 4, "needs_user_action": 2}
    assert robot["all_existing_attempts"]["jobs"] == 15
    assert robot["all_existing_attempts"]["status_counts"] == {"needs_review": 5, "needs_user_action": 3, "succeeded": 7}
    assert robot["canonical"]["extra_jobs_per_execution"] == 0
    assert robot["canonical"]["duplicate_external_effect_count"] is None
    assert len(robot["earlier_failures_retained"]) == 2
    assert robot["failure_reference_coverage"] == [
        {"path": "browser-actual-01/interrupted-summary.json", "present": False},
        {"path": "browser-diagnosis-02/pytest.xml", "present": True}]


def test_robot_nested_keywords_and_not_run_have_correct_readback_denominator(bundle):
    robot = bundle["metrics"]["sources"]["robot_loopback"]
    assert robot["canonical"]["readback_pass_rate_among_attempted"] == metrics.fraction(4, 9)
    values = [r for r in bundle["latencies"] if r["source_id"] == "robot_loopback"
              and r.get("canonical") is True and r["stage"] == "browser_readback"]
    assert len(values) == 11
    assert sum(r["duration_ms"] is not None for r in values) == 9
    assert all(r["duration_ms"] > 0 for r in values if r["duration_ms"] is not None)
    assert all(metrics.elapsed_ms(r["span_start"], r["span_end"]) == r["duration_ms"] for r in values)
    assert all(r["clock"] == "robot_xml_same_file" for r in values)


def test_missing_timestamps_are_not_estimated_from_other_spans(bundle):
    assert all(r["duration_ms"] is None for r in bundle["latencies"] if r["stage"] in {"end_to_end", "host_verification"})
    assert any(r["duration_ms"] is not None and r["terminal"] == "failed" for r in bundle["latencies"] if r["stage"] == "agent_active")
    assert all(r["duration_ms"] is None for r in bundle["latencies"] if r["source_id"] == "live_original" and r["terminal"] == "not_run")


def test_legacy_success_claims_are_not_certified_saves(bundle):
    legacy = bundle["metrics"]["sources"]["legacy_local"]
    assert legacy["message_details"] == 143
    assert legacy["message_duration_observed"] == 135
    assert legacy["save_action_status_claims"] == {"draft_saved": 6, "failed": 1, "needs_user_action": 2}
    assert legacy["task_observations"]["machine_verified_results"] == 0
    assert legacy["task_observations"]["automatic_completion_rate"]["value"] is None


def test_percentiles_keep_unknowns_and_empty_denominator():
    assert metrics.latency_summary([4, None, 1, 2, 3]) == {"observed": 4, "missing": 1, "p50_ms": 2, "p95_ms": 4}
    assert metrics.latency_summary([None])["p95_ms"] is None
    assert metrics.fraction(0, 0)["value"] is None
    assert metrics.fraction(0, 10, 1)["value"] is None
    assert metrics.elapsed_ms(None, "2026-11-01T00:00:00+00:00") is None
    assert metrics.elapsed_ms("2026-11-01T00:00:01+00:00", "2026-11-01T00:00:00+00:00") is None


def test_input_hash_change_and_duplicate_sources_fail_before_analysis(tmp_path):
    source = {"id": "one", "kind": "legacy", "path": "input.json", "label": "测试", "model": "unknown",
              "platform": "unknown", "executor": "unknown", "data_origin": "unknown"}
    data = tmp_path / "input.json"
    data.write_text("{}", "utf-8")
    catalog = {"schema_version": 1, "description": "测试", "sources": [source], "files_sha256": {"input.json": metrics.fingerprint(data)}}
    config = tmp_path / "sources.json"
    config.write_text(json.dumps(catalog), "utf-8")
    assert metrics.load_catalog(config, tmp_path).sources[0].id == "one"
    data.write_text('{"changed":true}', "utf-8")
    with pytest.raises(ValueError, match="哈希变化"):
        metrics.load_catalog(config, tmp_path)
    data.write_text("{}", "utf-8")
    catalog["sources"].append({**source, "id": "two"})
    config.write_text(json.dumps(catalog), "utf-8")
    with pytest.raises(ValueError, match="同一原始输入"):
        metrics.load_catalog(config, tmp_path)
    copied = tmp_path / "copied.json"
    copied.write_text("{}", "utf-8")
    catalog["sources"][1]["path"] = "copied.json"
    catalog["files_sha256"]["copied.json"] = metrics.fingerprint(copied)
    config.write_text(json.dumps(catalog), "utf-8")
    with pytest.raises(ValueError, match="副本不能重复"):
        metrics.load_catalog(config, tmp_path)


def test_query_connection_cannot_write_even_if_a_query_is_wrong(tmp_path):
    path = tmp_path / "read-only.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE evidence (id INTEGER)")
    before = metrics.fingerprint(path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        metrics.query_rows(path, "INSERT INTO evidence VALUES (1)")
    assert metrics.fingerprint(path) == before


def test_duplicate_platform_evidence_is_not_inferred_from_jobs():
    job = {"execution_id": "execution", "job_id": "job-1", "job_status": "succeeded",
           "readback_verified": True, "readback_attempted": True}
    result = metrics.summarize_jobs([job, {**job, "job_id": "job-2"}])
    assert result["extra_jobs_per_execution"] == 1
    assert result["duplicate_external_effect_count"] is None


def test_outputs_round_trip_and_never_overwrite(bundle, tmp_path):
    catalog = metrics.load_catalog()
    output = tmp_path / "result"
    metrics.write_outputs(bundle, catalog, metrics.DEFAULT_SOURCES, output)
    assert json.loads((output / "metrics.json").read_text("utf-8")) == bundle["metrics"]
    manifest = json.loads((output / "manifest.json").read_text("utf-8"))
    assert all(metrics.fingerprint(output / name) == digest for name, digest in manifest["output_sha256"].items())
    assert set(manifest["output_sha256"]) == {"tasks.csv", "jobs.csv", "latencies.csv", "metrics.json", "report.md"}
    assert "真实外部平台保存成功率=null" in (output / "report.md").read_text("utf-8")
    with pytest.raises(FileExistsError):
        metrics.write_outputs(bundle, catalog, metrics.DEFAULT_SOURCES, output)
