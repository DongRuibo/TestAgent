"""检验工具链评测的证据、反例判分及报告口径。"""

from copy import deepcopy
import json
from types import SimpleNamespace

from openai.types.chat import ChatCompletionMessage
import pytest

from evals import agent_runner as runner


@pytest.fixture(scope="module")
def evaluated(tmp_path_factory):
    output, report = runner.run_evaluation(report_root=tmp_path_factory.mktemp("agent-eval-report"))
    return runner.load_suite(), output, report


@pytest.mark.parametrize("case_id", [f"A{i:02}" for i in range(1, 7)] + [f"B{i:02}" for i in range(1, 9)])
def test_scripted_case_preserves_all_acceptance_evidence(evaluated, case_id):
    _, _, report = evaluated
    row = next(row for row in report["results"] if row["case_id"] == case_id)
    assert row["status"] == "passed", row
    assert all(all(t["score"]["checks"].values()) for t in row["turns"])


@pytest.mark.parametrize("corruption", ["no_execution", "wrong_fact", "fake_source", "wrong_call_id", "wrong_observation", "extra_read"])
def test_scorer_rejects_plausible_but_unsupported_success(evaluated, corruption):
    suite, _, report = evaluated
    case = next(c for c in suite.cases if c.id == "A02")
    observation = deepcopy(next(r for r in report["results"] if r["case_id"] == "A02")["turns"][0])
    payload = json.loads(observation["answer"])
    if corruption == "no_execution":
        observation["events"] = [e for e in observation["events"] if e["event"] != "handler_execution"]
    elif corruption == "wrong_fact":
        payload["quantity"] = 700
        observation["answer"] = json.dumps(payload)
    elif corruption == "fake_source":
        payload["sources"] = ["invented.md"]
        observation["answer"] = json.dumps(payload)
    elif corruption in {"wrong_call_id", "wrong_observation"}:
        attempt = next(e for e in observation["events"] if e["event"] == "sdk_attempt" and e["round"] == 2)
        message = next(m for m in attempt["request"]["messages"] if m["role"] == "tool")
        if corruption == "wrong_call_id":
            message["tool_call_id"] = "wrong"
        else:
            message["content"] = "不是 JSON 的伪造观察"
    else:
        policy = deepcopy(next(e for e in observation["events"] if e["event"] == "policy_result"))
        policy.update(call_id="extra", arguments='{"path":"shipping.md"}')
        observation["events"].append(policy)
    score = runner.score_turn(case.turns[0].expected, observation)
    assert score["passed"] is False


def test_unrelated_exception_is_not_expected_failure(evaluated):
    suite, _, report = evaluated
    case = next(c for c in suite.cases if c.id == "B07")
    observation = deepcopy(next(r for r in report["results"] if r["case_id"] == "B07")["turns"][0])
    observation["error"] = {"type": "ValueError", "message": "不是步数上限异常"}
    assert runner.score_turn(case.turns[0].expected, observation)["passed"] is False


def test_deduplication_does_not_hide_duplicate_handler_execution(evaluated):
    suite, _, report = evaluated
    case = next(c for c in suite.cases if c.id == "B04")
    observation = deepcopy(next(r for r in report["results"] if r["case_id"] == "B04")["turns"][0])
    execution = deepcopy(next(e for e in observation["events"] if e["event"] == "handler_execution"))
    execution["call_id"] = "B04-duplicate"
    observation["events"].append(execution)
    assert runner.score_turn(case.turns[0].expected, observation)["passed"] is False


@pytest.mark.parametrize("formatted", ['{ "path" : "order.md" }', '{\n "path": "order.md"\n}', '{"path":"\\u006frder.md"}'])
def test_dedupe_compares_json_values_and_preserves_raw_arguments(formatted):
    suite = runner.load_suite()
    case = next(c for c in suite.cases if c.id == "B04").model_copy(deep=True)
    original = case.turns[0].script[0].tool_calls[0].arguments
    case.turns[0].script[0].tool_calls[1].arguments = formatted
    row = runner.run_case(suite, case, "scripted")
    events = row["turns"][0]["events"]
    policies = [e for e in events if e["event"] == "policy_result"]
    assert [e["arguments"] for e in policies] == [original, formatted]
    assert sum(e["event"] == "handler_execution" for e in events) == 1
    assert policies[1]["result"]["_agent_execution"]["status"] == "deduplicated"
    assert row["status"] == "passed", row["turns"][0]["score"]


def test_dedupe_still_rejects_different_argument_values(evaluated):
    suite, _, report = evaluated
    case = next(c for c in suite.cases if c.id == "B04")
    observation = deepcopy(next(r for r in report["results"] if r["case_id"] == "B04")["turns"][0])
    policies = [e for e in observation["events"] if e["event"] == "policy_result"]
    policies[1]["arguments"] = '{"path":"shipping.md"}'
    score = runner.score_turn(case.turns[0].expected, observation)
    assert score["checks"]["dedupe.1"] is False


def test_file_error_must_be_missing_file_not_an_arbitrary_failure(evaluated):
    suite, _, report = evaluated
    case = next(c for c in suite.cases if c.id == "A05")
    observation = deepcopy(next(r for r in report["results"] if r["case_id"] == "A05")["turns"][0])
    next(e for e in observation["events"] if e["event"] == "policy_result")["result"]["error_type"] = "READ_ERROR"
    assert runner.score_turn(case.turns[0].expected, observation)["checks"]["policy_errors"] is False


def test_path_refusal_must_not_leak_the_outside_file(evaluated):
    suite, _, report = evaluated
    case = next(c for c in suite.cases if c.id == "B03")
    observation = deepcopy(next(r for r in report["results"] if r["case_id"] == "B03")["turns"][0])
    next(e for e in observation["events"] if e["event"] == "handler_execution")["result"]["content"] = suite.outside_text
    assert runner.score_turn(case.turns[0].expected, observation)["checks"]["forbidden_text.0"] is False


def test_valid_workspace_prefix_is_not_scored_as_an_extra_file():
    suite = runner.load_suite()
    case = suite.cases[1].model_copy(deep=True)
    case.turns[0].script[0].tool_calls[0].arguments = '{"path":"workspace/order.md"}'
    assert runner.run_case(suite, case, "scripted")["status"] == "passed"


def test_missing_case_evidence_stays_in_denominators(evaluated):
    _, _, report = evaluated
    rows = deepcopy(report["results"])
    old = rows[1]
    rows[1] = {key: value for key, value in old.items() if key not in {"turns", "duration_ms"}}
    rows[1]["status"] = "not_run"
    summary = runner.aggregate_agent_results(rows, "scripted")
    assert summary["case_pass_rate"] == 13 / 14
    assert summary["statuses"]["not_run"] == 1
    assert summary["fact_field_accuracy"] < 1
    assert summary["source_pass_rate"] < 1
    rows[1]["status"] = "harness_error"
    assert runner.aggregate_agent_results(rows, "scripted")["case_pass_rate"] == 13 / 14


def test_report_snapshots_usage_and_mode_are_honest(evaluated):
    _, output, report = evaluated
    summary = report["summary"]
    assert summary["passed"] == summary["planned"] == 14
    assert summary["task_success_rate"] is None
    assert summary["normal_case_median_ms"] is None
    assert summary["observed_token_usage"] is None
    assert summary["usage_missing_attempts"] == summary["sdk_attempts"]
    assert summary["cost_cny"] is None
    assert summary["deduplications"] == 1
    assert summary["policy_rejections"] == 3
    retry = next(r for r in report["results"] if r["case_id"] == "B08")
    assert sum(e["event"] == "model_round" for e in retry["turns"][0]["events"]) == 1
    assert sum(e["event"] == "sdk_attempt" for e in retry["turns"][0]["events"]) == 3
    first = next(r for r in report["results"] if r["case_id"] == "A02")["turns"][0]
    attempts = [e for e in first["events"] if e["event"] == "sdk_attempt"]
    assert not any(m["role"] == "tool" for m in attempts[0]["request"]["messages"])
    assert any(m["role"] == "tool" for m in attempts[1]["request"]["messages"])
    assert json.loads((output / "report.json").read_text())["summary"] == summary
    assert json.loads((output / "manifest.json").read_text())["source_sha256"]
    assert (output / "report.md").is_file()
    records = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    assert len(records) == sum(len(t["events"]) for r in report["results"] for t in r["turns"])


def test_scripted_mode_does_not_initialize_live_configuration(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("离线模式不得初始化真实模型")
    monkeypatch.setattr(runner.LLMConfig, "from_env", forbidden)
    monkeypatch.setattr(runner, "OpenAI", forbidden)
    _, report = runner.run_evaluation(case_ids=["A02"], repetitions=2, report_root=tmp_path)
    assert report["summary"]["passed"] == 2


def test_suite_validation_is_at_entry_and_live_filters_runtime_cases(tmp_path):
    source = json.loads(runner.CASE_PATH.read_text())
    source["files"]["../unsafe.md"] = "不能作为夹具写到工作区外"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(source))
    with pytest.raises(ValueError):
        runner.load_suite(path)
    with pytest.raises(ValueError, match="不支持"):
        runner.run_evaluation(mode="live", case_ids=["B08"], report_root=tmp_path)


def test_exhausted_script_is_harness_error_not_agent_success():
    suite = runner.load_suite()
    case = suite.cases[1].model_copy(deep=True)
    case.turns[0].script = []
    row = runner.run_case(suite, case, "scripted")
    assert row["status"] == "harness_error"


def test_mixed_modes_cannot_share_a_summary(evaluated):
    _, _, report = evaluated
    rows = deepcopy(report["results"])
    rows[0]["mode"] = "live"
    with pytest.raises(ValueError, match="不能混合"):
        runner.aggregate_agent_results(rows, "scripted")


@pytest.mark.parametrize("mode,delegate", [("live", None), ("scripted", object()), ("live", object())])
def test_case_entry_cannot_label_scripted_responses_as_live(mode, delegate):
    suite = runner.load_suite()
    with pytest.raises(ValueError, match="必须提供"):
        runner.run_case(suite, suite.cases[0], mode, delegate)


def test_live_sdk_interface_and_usage_with_offline_delegate():
    suite = runner.load_suite()
    case = suite.cases[1]
    replies = iter(case.turns[0].script)
    usage = SimpleNamespace(model_dump=lambda **kwargs: {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25})

    class OfflineDelegate:
        # 仅验证真实模式的接口形状和用量采集，绝不建立网络连接。
        def __init__(self):
            self.chat = self.completions = self

        def create(self, **request):
            reply = next(replies)
            message = ChatCompletionMessage(role="assistant", content=reply.content, tool_calls=[
                {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                for call in reply.tool_calls])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

    row = runner.run_case(suite, case, "live", OfflineDelegate(), runner.LLMConfig("synthetic-secret", "offline-interface-test"))
    summary = runner.aggregate_agent_results([row], "live")
    assert row["status"] == "passed"
    assert summary["task_success_rate"] == 1
    assert summary["observed_token_usage"]["total_tokens"] == 50
    assert summary["normal_case_median_ms"] >= 0
    assert "synthetic-secret" not in json.dumps(row)


def test_keyboard_interrupt_keeps_unrun_cases_in_saved_report(tmp_path, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(runner, "run_case", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runner.run_evaluation(case_ids=["A01", "A02"], report_root=tmp_path)
    report = json.loads(next(tmp_path.glob("*/report.json")).read_text())
    assert report["summary"]["planned"] == 2
    assert report["summary"]["statuses"]["not_run"] == 2
    assert report["summary"]["case_pass_rate"] == 0


@pytest.mark.parametrize("scenario", ["sdk", "second_turn", "handler"])
def test_interrupt_preserves_partial_case_events_and_usage(tmp_path, monkeypatch, scenario):
    suite = runner.load_suite()
    case_id = "A04" if scenario == "second_turn" else "A02"
    case = next(c for c in suite.cases if c.id == case_id)
    replies = iter(case.turns[0].script)
    usage = SimpleNamespace(model_dump=lambda **kwargs: {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25})

    class InterruptingSDK:
        # 本地替身在实际记录 SDK 尝试后中断，验证真实模式报告路径但不联网。
        def __init__(self):
            self.chat = self.completions = self
            self.calls = 0
            self.closed = False

        def create(self, **request):
            self.calls += 1
            limit = 3 if scenario == "second_turn" else 2
            if self.calls == limit:
                raise KeyboardInterrupt
            reply = next(replies)
            message = ChatCompletionMessage(role="assistant", content=reply.content, tool_calls=[
                {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                for call in reply.tool_calls])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

        def close(self):
            self.closed = True

    sdk = InterruptingSDK()
    monkeypatch.setattr(runner, "OpenAI", lambda **kwargs: sdk)
    monkeypatch.setattr(runner.LLMConfig, "from_env", lambda: runner.LLMConfig("synthetic-secret", "offline-interrupt-test"))
    if scenario == "handler":
        def interrupt_read(*args, **kwargs):
            raise KeyboardInterrupt
        monkeypatch.setattr(runner, "read_file", interrupt_read)
    with pytest.raises(KeyboardInterrupt):
        runner.run_evaluation(mode="live", case_ids=[case_id, "A06"], report_root=tmp_path)
    folder = next(tmp_path.glob("live-*"))
    report = json.loads((folder / "report.json").read_text())
    row, future = report["results"]
    summary = report["summary"]
    expected_calls = 3 if scenario == "second_turn" else 1 if scenario == "handler" else 2
    assert sdk.closed and sdk.calls == expected_calls
    assert row["status"] == "interrupted"
    assert future["status"] == "not_run" and not future.get("turns")
    assert summary["statuses"]["interrupted"] == summary["statuses"]["not_run"] == 1
    assert summary["case_pass_rate"] == summary["source_pass_rate"] == summary["tool_contract_pass_rate"] == 0
    assert summary["sdk_attempts"] == expected_calls
    assert summary["usage_observed_attempts"] == (2 if scenario == "second_turn" else 1)
    assert summary["usage_missing_attempts"] == (0 if scenario == "handler" else 1)
    assert summary["observed_token_usage"]["total_tokens"] == (50 if scenario == "second_turn" else 25)
    assert summary["tool_requests"] == summary["handler_executions"] == 1
    assert row["turns"][-1]["error"]["type"] == "KeyboardInterrupt"
    assert not row["turns"][-1]["score"]["passed"]
    if scenario == "second_turn":
        assert row["turns"][0]["score"]["passed"]
    records = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    assert len(records) == sum(len(t["events"]) for t in row["turns"])
    assert all(e["case_id"] == case_id for e in records)
    assert "synthetic-secret" not in json.dumps(report)
