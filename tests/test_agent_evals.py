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
    monkeypatch.setattr(runner.LLMConfig, "from_env", lambda: runner.LLMConfig("synthetic-secret", "offline-interrupt-test", "https://eval.invalid/v1"))
    if scenario == "handler":
        def interrupt_read(*args, **kwargs):
            raise KeyboardInterrupt
        monkeypatch.setattr(runner, "read_file", interrupt_read)
    with pytest.raises(KeyboardInterrupt):
        runner.run_evaluation(mode="live", case_ids=[case_id, "A06"], report_root=tmp_path,
                              approval_path=live_approval(tmp_path), ledger_path=tmp_path / "ledger.json")
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


def live_approval(tmp_path, **changes):
    value = {"schema_version": 1, "provider": "offline-test", "model": "offline-interrupt-test",
        "base_url": "https://eval.invalid/v1", "price_source": "https://eval.invalid/pricing",
        "input_bound_source": "https://eval.invalid/model", "verification_evidence_sha256": "0" * 64,
        "ledger_path": str(tmp_path / "ledger.json"), "verified_at": "2026-11-06", "input_bound_basis": "离线夹具：不是供应商定价证明",
        "input_cny_per_million": "2", "output_cny_per_million": "8", "max_input_tokens": 1100000,
        "max_billable_output_tokens": 1100000, "max_request_bytes": 500000, "max_output_tokens": 8192,
        "max_attempts": 200, "max_seconds": 3600, "request_timeout_seconds": 30, "max_cny": "20", **changes}
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(value), "utf-8")
    return path


def test_live_requires_persisted_budget_before_sdk_construction(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.LLMConfig, "from_env", lambda: runner.LLMConfig("synthetic", "offline-interrupt-test"))
    monkeypatch.setattr(runner, "OpenAI", lambda **kw: pytest.fail("预算未复核不能创建网络客户端"))
    with pytest.raises(ValueError, match="approval"):
        runner.run_evaluation(mode="live", report_root=tmp_path)


def test_budget_is_shared_across_sdk_instances_and_restart(tmp_path):
    from evals.live_budget import LiveBudget
    approval = live_approval(tmp_path)
    ledger = tmp_path / "ledger.json"
    budget = LiveBudget(approval, ledger, model="offline-interrupt-test", base_url="https://eval.invalid/v1")
    request = {"model": "offline-interrupt-test", "messages": []}
    first = budget.reserve(request, {"runner": "agent"})
    budget.settle(first, {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25})
    reopened = LiveBudget(approval, ledger, model="offline-interrupt-test", base_url="https://eval.invalid/v1")
    second = reopened.reserve(request, {"runner": "content"})
    reopened.settle(second, None, "APITimeoutError")
    assert reopened.snapshot()["sdk_attempts"] == 2
    assert reopened.snapshot()["committed_upper_cny"] == "11.00008"
    resumed = LiveBudget(approval, ledger, model="offline-interrupt-test", base_url="https://eval.invalid/v1")
    with pytest.raises(runner.BudgetStopped, match="unknown_usage"):
        resumed.reserve(request, {"runner": "retest"})
    assert len(resumed.data["attempts"]) == 2
    assert resumed.data["attempts"][1]["actual_cny"] is None


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 9},
    {"prompt_tokens": None, "completion_tokens": 2, "total_tokens": 12}])
def test_unknown_or_inconsistent_usage_stops_without_zero_cost(tmp_path, usage):
    from evals.live_budget import LiveBudget
    budget = LiveBudget(live_approval(tmp_path), tmp_path / "ledger.json",
                        model="offline-interrupt-test", base_url="https://eval.invalid/v1")
    item = budget.reserve({"model": "offline-interrupt-test", "messages": []}, {})
    budget.settle(item, usage)
    assert item["status"] == "unknown" and item["actual_cny"] is None
    assert str(budget.committed_cny) == "11"
    with pytest.raises(runner.BudgetStopped):
        budget.check()


def test_reserved_request_survives_process_loss(tmp_path):
    from evals.live_budget import LiveBudget
    approval = live_approval(tmp_path)
    ledger = tmp_path / "ledger.json"
    first = LiveBudget(approval, ledger, model="offline-interrupt-test", base_url="https://eval.invalid/v1")
    first.reserve({"model": "offline-interrupt-test", "messages": []}, {})
    resumed = LiveBudget(approval, ledger, model="offline-interrupt-test", base_url="https://eval.invalid/v1")
    assert resumed.snapshot()["unknown_attempts"] == 1
    assert resumed.snapshot()["committed_upper_cny"] == "11"
    with pytest.raises(runner.BudgetStopped, match="unresolved_reserved_attempt"):
        resumed.check()


def test_sdk_boundary_passes_actual_output_cap_and_thinking_flag(tmp_path):
    from evals.live_budget import LiveBudget
    budget = LiveBudget(live_approval(tmp_path), tmp_path / "ledger.json",
                        model="offline-interrupt-test", base_url="https://eval.invalid/v1")
    received = []
    class Delegate:
        def __init__(self):
            self.chat = self.completions = self
        def create(self, **request):
            received.append(request)
            return SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(role="assistant", content="READY"))],
                usage=SimpleNamespace(model_dump=lambda **kw: {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}))
    events = []
    sdk = runner.RecordedSDK(events, Delegate(), budget)
    sdk.create(model="offline-interrupt-test", messages=[])
    assert received[0]["max_tokens"] == 8192
    assert received[0]["extra_body"] == {"enable_thinking": False}
    assert events[0]["budget_attempt_id"] == 1 and events[0]["actual_cny"] == "0.00008"


def test_connection_failure_cannot_hide_retry_or_reuse_reserve(tmp_path):
    from evals.live_budget import LiveBudget
    budget = LiveBudget(live_approval(tmp_path), tmp_path / "ledger.json",
                        model="offline-interrupt-test", base_url="https://eval.invalid/v1")
    class Delegate:
        def __init__(self):
            self.chat = self.completions = self
            self.calls = 0
        def create(self, **request):
            self.calls += 1
            raise runner.APITimeoutError(request=runner.httpx.Request("POST", "https://eval.invalid"))
    delegate = Delegate()
    events = []
    sdk = runner.RecordedSDK(events, delegate, budget)
    client = runner.RecordedLLM(runner.LLMConfig("synthetic", "offline-interrupt-test"), sdk_client=sdk)
    with pytest.raises(runner.BudgetStopped, match="unknown_usage"):
        client.complete([])
    assert delegate.calls == sum(event["event"] == "sdk_attempt" for event in events) == len(budget.data["attempts"]) == 1
    assert next(event for event in events if event["event"] == "sdk_attempt")["usage"] is None
    assert budget.data["attempts"][0]["reserved_cny"] == "11"


def test_aggregate_preserves_inconsistent_usage_as_invalid_unknown(evaluated):
    _, _, report = evaluated
    row = deepcopy(next(row for row in report["results"] if row["case_id"] == "A01"))
    attempt = next(event for event in row["turns"][0]["events"] if event["event"] == "sdk_attempt")
    attempt["usage"] = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 9}
    summary = runner.aggregate_agent_results([row], "scripted")
    assert summary["usage_observed_attempts"] == 0
    assert summary["usage_invalid_attempts"] == summary["usage_missing_attempts"] == 1
    assert summary["observed_token_usage"] is summary["cost_cny"] is None
    assert attempt["usage"]["total_tokens"] == 9


def test_invalid_usage_is_saved_and_stops_later_case_without_becoming_pass(tmp_path, monkeypatch):
    class InvalidUsageSDK:
        def __init__(self):
            self.chat = self.completions = self
            self.calls = 0
            self.closed = False
        def create(self, **request):
            self.calls += 1
            return SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(role="assistant", content="READY"))],
                usage=SimpleNamespace(model_dump=lambda **kw: {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 9}))
        def close(self):
            self.closed = True
    sdk = InvalidUsageSDK()
    monkeypatch.setattr(runner, "OpenAI", lambda **kw: sdk)
    monkeypatch.setattr(runner.LLMConfig, "from_env", lambda: runner.LLMConfig("synthetic", "offline-interrupt-test", "https://eval.invalid/v1"))
    output, report = runner.run_evaluation(mode="live", case_ids=["A01", "A02"], report_root=tmp_path / "reports",
        approval_path=live_approval(tmp_path), ledger_path=tmp_path / "ledger.json")
    assert sdk.calls == 1 and sdk.closed
    saved = json.loads((output / "report.json").read_text())
    assert saved["results"][0]["status"] == "failed"
    assert saved["results"][0]["turns"][0]["score"]["checks"]["usage_complete"] is False
    assert saved["results"][1]["status"] == "not_run" and saved["results"][1]["not_run_reason"] == "unknown_usage"
    assert saved["summary"]["usage_observed_attempts"] == 0
    assert saved["summary"]["usage_invalid_attempts"] == saved["summary"]["usage_missing_attempts"] == 1
    assert saved["summary"]["cost_cny"] is saved["summary"]["observed_token_usage"] is None
    assert saved["summary"]["shared_budget"]["committed_upper_cny"] == "11"
    assert saved["summary"]["shared_budget"]["unknown_attempts"] == 1
    event = next(json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()
                 if json.loads(line)["event"] == "sdk_attempt")
    assert event["usage"] == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 9}


@pytest.mark.parametrize("finish_reason", [None, "stop", "tool_calls", "length"])
def test_sdk_preserves_actual_finish_reason_without_inference(finish_reason):
    class Delegate:
        def __init__(self): self.chat = self.completions = self
        def create(self, **request):
            choice = SimpleNamespace(message=ChatCompletionMessage(role="assistant", content="即将保存，但没有工具调用。"))
            if finish_reason is not None:
                choice.finish_reason = finish_reason
            return SimpleNamespace(choices=[choice], usage=None)
    events = []
    runner.RecordedSDK(events, Delegate()).create(model="offline", messages=[])
    assert events[0]["finish_reason"] == finish_reason


def quota_fixture(tmp_path):
    from evals.live_budget import LiveBudget
    root = tmp_path / "evidence"
    root.mkdir()
    path = live_approval(root, max_attempts=100, max_seconds=1800, ledger_path=str(root / "actual/shared-ledger.json"))
    path.rename(root / "proposed-approval.json")
    budget = LiveBudget(root / "proposed-approval.json", root / "actual/shared-ledger.json",
        model="offline-interrupt-test", base_url="https://eval.invalid/v1")
    for _ in range(100):
        item = budget.reserve({"model": "offline-interrupt-test", "messages": []}, {})
        budget.settle(item, {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
    with pytest.raises(runner.BudgetStopped, match="sdk_attempt_limit"):
        budget.check()
    report = root / "actual/results/offline-first-batch"
    report.mkdir(parents=True)
    for name in ("manifest.json", "report.json", "report.md", "events.jsonl"):
        (report / name).write_text("original-byte-snapshot\n", "utf-8")
    return root, budget


def test_quota_proposal_and_one_apply_preserve_history_clock_money_and_reports(tmp_path):
    from evals import revise_acceptance_quota as revision
    from evals.live_budget import LiveBudget
    root, budget = quota_fixture(tmp_path)
    approval_path, ledger_path = root / "proposed-approval.json", root / "actual/shared-ledger.json"
    approval_bytes, ledger_bytes = approval_path.read_bytes(), ledger_path.read_bytes()
    old = json.loads(ledger_bytes)
    proposal = tmp_path / "proposal"
    manifest = revision.prepare(root, proposal)
    assert approval_path.read_bytes() == approval_bytes and ledger_path.read_bytes() == ledger_bytes
    assert manifest["status"] == "proposal_only_not_applied"
    assert manifest["revision"]["applies_from_attempt_id"] == 101
    assert manifest["revision"]["case_runs"] == [{"case_id": f"L{i:02}", "repetition": rep} for rep in (1, 2) for i in range(1, 5)]
    assert (proposal / "first-batch-snapshot/proposed-approval.json").read_bytes() == approval_bytes
    assert (proposal / "first-batch-snapshot/shared-ledger.json").read_bytes() == ledger_bytes
    receipt = revision.apply(proposal)
    new = json.loads(ledger_path.read_text())
    assert receipt["status"] == "applied_no_model_called" and new["attempts"] == old["attempts"]
    assert new["started_at_epoch"] == old["started_at_epoch"]
    assert new["approval"]["max_attempts"] == 300 and new["approval"]["max_seconds"] == 3600
    assert {key for key in new["approval"] if new["approval"][key] != old["approval"][key]} == {"max_attempts", "max_seconds"}
    reopened = LiveBudget(approval_path, ledger_path, model="offline-interrupt-test", base_url="https://eval.invalid/v1")
    assert reopened.committed_cny == budget.committed_cny and len(reopened.data["attempts"]) == 100
    next_item = reopened.reserve({"model": "offline-interrupt-test", "messages": []}, {})
    assert next_item["attempt_id"] == 101
    with pytest.raises(ValueError): revision.apply(proposal)
    assert (root / "actual/results/offline-first-batch/report.json").read_text() == "original-byte-snapshot\n"


@pytest.mark.parametrize("corruption", ["unknown", "reserved", "other_halt", "clock_expired", "price_tampered"])
def test_quota_revision_refuses_unknown_unapproved_change_or_expired_original_clock(tmp_path, monkeypatch, corruption):
    from evals import revise_acceptance_quota as revision
    root, _ = quota_fixture(tmp_path)
    path = root / "actual/shared-ledger.json"
    data = json.loads(path.read_text())
    if corruption in {"unknown", "reserved"}: data["attempts"][0]["status"] = corruption
    elif corruption == "other_halt": data["halted_reason"] = "unknown_usage"
    elif corruption == "clock_expired": data["started_at_epoch"] -= 4000
    elif corruption == "price_tampered": data["approval"]["input_cny_per_million"] = "1"
    path.write_text(json.dumps(data), "utf-8")
    original = path.read_bytes()
    proposal = tmp_path / "proposal"
    if corruption == "clock_expired":
        revision.prepare(root, proposal)
        with pytest.raises(ValueError, match="3600"):
            revision.apply(proposal)
    else:
        with pytest.raises(ValueError): revision.prepare(root, proposal)
    assert path.read_bytes() == original
