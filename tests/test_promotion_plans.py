"""投流计划本地验收：真实导出结构、预算/来源边界、不可变版本和 Agent 工具链。"""

from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agent.loop import AgentLoop, DEFAULT_TOOL_BUDGETS, DEFAULT_TOOL_HANDLERS, TOOL_DEFINITIONS
from agent.skill_tools import register_skill_tools
from agent.skills import SkillCatalog
from testagent_dev_agent.paths import REPOSITORY_ROOT
from tools import promotion_plans as plans, content_research, operation_goals
from tools.content_research import ContentResearchTools
from tools.operation_goals import GoalSaveRequest, OperationGoalStore
from test_agent_loop import FakeLLMClient, FakeMessage, make_tool_call
from test_content_research import _valid_result
from test_operation_goal_handoff import goal_input
from test_xiaohongshu_metrics import export_fixture, parse, source_row


SKILL_NAME = "xiaohongshu-promotion-planning"


def promotion_agent(client):
    catalog = SkillCatalog.from_directory(REPOSITORY_ROOT / ".agents" / "skills")
    return AgentLoop(client, trace=None, skill_catalog=catalog,
                     tool_handlers=register_skill_tools(DEFAULT_TOOL_HANDLERS, catalog))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(plans, "collect_xiaohongshu_metrics",
                        lambda: pytest.fail("显式文件或参数错误不得触发浏览器采集"))
    monkeypatch.setattr(plans, "PROMOTION_DIR", tmp_path / "plans")
    monkeypatch.setattr(plans, "METRICS_DIR", tmp_path / "metrics")
    monkeypatch.setattr(content_research, "CONTENT_RESEARCH_OUTPUT_DIR", tmp_path / "research")
    monkeypatch.setattr(operation_goals, "OPERATION_GOAL_DIR", tmp_path / "goals")
    metrics = parse(export_fixture(tmp_path / "official.xlsx"))
    path = plans.METRICS_DIR / "sample" / "metrics.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(metrics, ensure_ascii=False), "utf-8")
    return {"tmp": tmp_path, "metrics": metrics, "path": path}


def prepare(env, **changes):
    values = {"metrics_artifact_path": str(env["path"]), "company_name": "样例设计企业",
        "company_profile": "上海家装设计企业，服务小户型翻新。", "objective": "测试能否获得有效咨询",
        "primary_metric": "inquiries", "regions": ["成都"], "target_audience": ["有装修意向的业主"],
        "start_date": "2026-09-20", "end_date": "2026-09-22", "budget_cap_cny": "100.00",
        "budget_basis": "仅用于测试的模拟预算，不构成真实授权"}
    values.update(changes)
    return plans.prepare_promotion_plan(**values)


def proposal(prepared, **changes):
    values = {"input_artifact_path": prepared["input_artifact_path"],
        "summary": "模拟数据计划：先验证咨询意向，全部等待人工审批。",
        "assumptions": ["预算和规则均为测试样例，不是用户的真实经营约束。"],
        "candidates": [{"post_index": 0,
            "post_url": "https://www.xiaohongshu.com/explore/" + "a" * 24,
            "reason": "样例笔记具备观看与互动记录，可作为测试候选；咨询情况未知。",
            "hypothesis": "测试扩大触达后是否能获得目标咨询。", "budget_cny": "40.00",
            "expected_result": {"description": "验证是否出现有效咨询", "basis": "缺少历史投放数据，属于待验证假设。"},
            "stop_conditions": [{"condition": "达到本轮预算上限后结束测试。", "required_data": ["spend"]}],
            "additional_budget_conditions": [{"condition": "有有效线索后由负责人复核成本并重新审批。", "required_data": ["spend", "leads"]}]}]}
    values.update(changes)
    return values


def saved_data(result):
    assert result["success"], result
    return json.loads(Path(result["artifact_path"]).read_text("utf-8"))


def test_end_to_end_preserves_nulls_uses_baseline_and_writes_report(env):
    prepared = prepare(env)
    assert prepared["success"], prepared
    assert prepared["missing_information"] == ["market_research"]
    assert prepared["metrics_context"]["collected_at"] == env["metrics"]["collected_at"]
    assert prepared["metrics_context"]["traffic_scope"] == "unspecified_by_export"
    posts = plans.load_promotion_plan(artifact_path=prepared["input_artifact_path"], section="posts")
    assert posts["items"][0]["post_index"] == 0
    assert posts["items"][0]["saves"] == 0
    assert posts["items"][0]["spend"] is None
    result = plans.save_promotion_plan(**proposal(prepared))
    data = saved_data(result)
    assert data["approval_status"] == "pending_approval"
    assert data["readiness"] == "needs_information"
    assert data["budget"]["total"] == "40.00"
    assert data["budget"]["unallocated"] == "60.00"
    candidate = data["candidates"][0]
    assert candidate["baseline"]["views"] == 100
    assert candidate["baseline"]["cover_click_rate"] == .1
    assert candidate["baseline"]["inquiries"] is None
    assert candidate["expected_result"]["kind"] == "test_target_not_forecast"
    assert candidate["additional_budget_conditions"][0]["missing_data"] == ["spend", "leads"]
    assert candidate["additional_budget_conditions"][0]["evaluation_status"] == "not_evaluated"
    report = Path(result["report_path"]).read_text("utf-8")
    for label in ("建议投流内容", "建议原因", "目标人群", "地区", "周期", "预算", "数据基线", "预期结果", "止损条件", "追加预算条件"):
        assert label in report
    assert "花费 | 待补充" in report
    assert "封面点击率 | 10.00%" in report
    assert "- 市场研究" in report and "market_research" not in report
    loaded = plans.load_promotion_plan(artifact_path=result["artifact_path"])
    assert loaded["version"] == 1
    assert loaded["budget"] == data["budget"]


def test_missing_constraints_and_budgets_are_not_filled_with_zero(env):
    prepared = plans.prepare_promotion_plan(metrics_artifact_path=str(env["path"]))
    assert prepared["success"]
    assert set(prepared["missing_information"]) >= {"company_name", "objective", "primary_metric", "budget_cap_cny", "regions"}
    value = proposal(prepared)
    value["candidates"][0].update(budget_cny=None, post_url=None)
    result = plans.save_promotion_plan(**value)
    data = saved_data(result)
    assert data["budget"]["total"] is data["budget"]["unallocated"] is None
    assert data["readiness"] == "needs_information"
    assert "posts[0].post_url" in result["missing_information"]
    assert "posts[0].budget_cny" in result["missing_information"]


@pytest.mark.parametrize("recommendation", ["hold", "optimize_first"])
def test_non_spending_advice_can_be_saved_without_forced_budget_or_rules(env, recommendation):
    prepared = prepare(env)
    value = proposal(prepared)
    value["candidates"][0].update(recommendation=recommendation, budget_cny=None,
                                  stop_conditions=[], additional_budget_conditions=[])
    result = plans.save_promotion_plan(**value)
    data = saved_data(result)
    assert data["budget"]["total"] == "0"
    assert data["budget"]["unallocated"] == "100.00"
    assert "posts[0].budget_cny" not in data["missing_information"]
    assert data["candidates"][0]["baseline"] == env["metrics"]["posts"][0]
    report = Path(result["report_path"]).read_text()
    assert "本轮不分配" in report and "本轮未建议投流，不适用" in report
    value["candidates"][0]["budget_cny"] = "10.00"
    assert not plans.save_promotion_plan(**value)["success"]


@pytest.mark.parametrize("recommendation", ["prioritize", "test"])
def test_spending_advice_still_requires_stop_and_addition_conditions(env, recommendation):
    value = proposal(prepare(env))
    value["candidates"][0].update(recommendation=recommendation, stop_conditions=[])
    assert not plans.save_promotion_plan(**value)["success"]


def test_suggested_settings_never_fill_unknown_input_constraints(env):
    prepared = plans.prepare_promotion_plan(metrics_artifact_path=str(env["path"]))
    original = Path(prepared["input_artifact_path"]).read_bytes()
    value = proposal(prepared)
    value["planning_suggestions"] = "建议预算50元，仅为待确认假设；地区与周期需补充依据。"
    # 文字建议不能使未知预算上限变成可分配额度。
    assert not plans.save_promotion_plan(**value)["success"]
    value["candidates"][0]["budget_cny"] = None
    result = plans.save_promotion_plan(**value)
    data = saved_data(result)
    assert data["input_snapshot"]["business"]["budget_cap_cny"] is None
    assert data["budget"]["total"] is data["budget"]["cap"] is None
    assert data["readiness"] == "needs_information"
    assert data["planning_suggestions"] == value["planning_suggestions"]
    assert Path(prepared["input_artifact_path"]).read_bytes() == original
    overview = plans.load_promotion_plan(artifact_path=result["artifact_path"])
    assert overview["planning_suggestions"] == value["planning_suggestions"]
    assert "建议设置（待确认，不替代上方输入约束）" in Path(result["report_path"]).read_text()


@pytest.mark.parametrize("bad", [True, False, "NaN", "Infinity", -1, 0, "0.001"])
def test_invalid_money_is_rejected(env, bad):
    assert not prepare(env, budget_cap_cny=bad)["success"]
    prepared = prepare(env)
    value = proposal(prepared)
    value["candidates"][0]["budget_cny"] = bad
    assert not plans.save_promotion_plan(**value)["success"]


def test_all_allocations_checked_even_when_another_budget_is_missing(env):
    prepared = prepare(env)
    value = proposal(prepared)
    value["candidates"][0]["budget_cny"] = "100.01"
    assert "预算" in plans.save_promotion_plan(**value)["message"]
    value["candidates"].append(deepcopy(value["candidates"][0]))
    value["candidates"][1]["budget_cny"] = None
    assert not plans.save_promotion_plan(**value)["success"]
    assert not list(plans.PROMOTION_DIR.glob("*/v*/plan.json"))


@pytest.mark.parametrize("extra", [{"approval_status": "approved"}, {"version": 7}, {"plan_id": str(uuid4())}, {"baseline": {"leads": 9}}])
def test_model_cannot_supply_authority_identity_or_baseline(env, extra):
    prepared = prepare(env)
    assert not plans.save_promotion_plan(**proposal(prepared), **extra)["success"]


@pytest.mark.parametrize("change", [{"post_index": 1}, {"post_index": True}, {"research_source_refs": ["made-up"]},
    {"baseline": {"leads": 9}}, {"post_url": "https://evil.example/abc"},
    {"post_url": "https://www.xiaohongshu.com/explore/" + "a" * 24 + "?xsec_token=secret"}])
def test_invalid_candidate_or_fabricated_source_is_rejected(env, change):
    value = proposal(prepare(env))
    value["candidates"][0].update(change)
    assert not plans.save_promotion_plan(**value)["success"]


def test_duplicate_candidates_and_date_order_fail(env):
    value = proposal(prepare(env))
    value["candidates"] *= 2
    assert not plans.save_promotion_plan(**value)["success"]
    assert not prepare(env, start_date="2026-09-25", end_date="2026-09-20")["success"]


@pytest.mark.parametrize("mutate", [lambda x: x.update(collected_count=9),
    lambda x: x["posts"][0].update(views=-1), lambda x: x["posts"][0].update(views=True),
    lambda x: x["posts"][0].update(cover_click_rate=2), lambda x: x["posts"][0].update(spend=0),
    lambda x: x.update(traffic_scope="natural"), lambda x: x.update(source=None),
    lambda x: x.update(account={}), lambda x: x.update(posts=None)])
def test_rejects_malformed_backend_data(env, mutate):
    value = deepcopy(env["metrics"])
    mutate(value)
    env["path"].write_text(json.dumps(value), "utf-8")
    assert not prepare(env)["success"]


def test_only_expected_artifact_paths_and_no_symlink_reads(env):
    outside = env["tmp"] / "outside.json"
    outside.write_text(json.dumps(env["metrics"]), "utf-8")
    assert not prepare(env, metrics_artifact_path=str(outside))["success"]
    linked = plans.METRICS_DIR / "linked"
    linked.symlink_to(env["path"].parent, target_is_directory=True)
    assert not prepare(env, metrics_artifact_path=str(linked / "metrics.json"))["success"]
    assert not plans.load_promotion_plan(artifact_path=str(outside))["success"]


def test_inputs_and_history_are_immutable_and_old_revision_cannot_overwrite(env):
    prepared = prepare(env)
    value = proposal(prepared)
    first = plans.save_promotion_plan(**value)
    old_bytes = Path(first["artifact_path"]).read_bytes()
    value.update(previous_plan_artifact_path=first["artifact_path"], revision_reason="调整测试分配")
    value["candidates"][0]["budget_cny"] = "60.00"
    second = plans.save_promotion_plan(**value)
    assert second["version"] == 2 and second["plan_id"] == first["plan_id"]
    assert second["approval_status"] == "pending_approval"
    assert Path(first["artifact_path"]).read_bytes() == old_bytes
    assert not plans.save_promotion_plan(**value)["success"]
    env["path"].unlink()
    # 历史方案读取冻结输入，不依赖后来改变或删除的原始数据。
    loaded = plans.load_promotion_plan(artifact_path=first["artifact_path"], section="candidates")
    assert loaded["items"][0]["baseline"]["views"] == 100
    assert loaded["items"][0]["budget_cny"] == "40.00"


def test_revision_requires_reason_and_same_account_and_company(env):
    prepared = prepare(env)
    first = plans.save_promotion_plan(**proposal(prepared))
    value = proposal(prepared, previous_plan_artifact_path=first["artifact_path"])
    assert not plans.save_promotion_plan(**value)["success"]
    other = prepare(env, company_name="另一企业")
    assert not plans.save_promotion_plan(**proposal(other, previous_plan_artifact_path=first["artifact_path"], revision_reason="修改"))["success"]
    metrics = deepcopy(env["metrics"])
    metrics["account"]["red_id"] = "another_account"
    env["path"].write_text(json.dumps(metrics), "utf-8")
    other = prepare(env)
    assert not plans.save_promotion_plan(**proposal(other, previous_plan_artifact_path=first["artifact_path"], revision_reason="修改"))["success"]


def test_tampered_snapshots_and_plans_fail(env):
    prepared = prepare(env)
    result = plans.save_promotion_plan(**proposal(prepared))
    for path, field in ((prepared["input_artifact_path"], "business"), (result["artifact_path"], "approval_status")):
        data = json.loads(Path(path).read_text("utf-8"))
        data[field] = "tampered"
        Path(path).write_text(json.dumps(data), "utf-8")
        assert not plans.load_promotion_plan(artifact_path=path)["success"]
    assert not plans.save_promotion_plan(**proposal(prepared))["success"]


def test_research_sources_and_company_are_checked(env):
    research = ContentResearchTools(output_dir=content_research.CONTENT_RESEARCH_OUTPUT_DIR).save_result(**_valid_result())
    assert research["success"]
    prepared = prepare(env, research_artifact_path=research["artifact_path"])
    assert prepared["missing_information"] == []
    value = proposal(prepared)
    value["candidates"][0]["research_source_refs"] = ["source-1"]
    data = saved_data(plans.save_promotion_plan(**value))
    assert data["readiness"] == "ready_for_review"
    assert data["approval_status"] == "pending_approval"
    source_page = plans.load_promotion_plan(artifact_path=prepared["input_artifact_path"], section="research", limit=1)
    assert source_page["next_offset"] == 1
    assert "固定离线样例" in source_page["items"][0]["text"]
    assert not prepare(env, research_artifact_path=research["artifact_path"], company_name="其他企业")["success"]
    value["candidates"][0]["research_source_refs"] = []
    assert "research_evidence" in str(plans.save_promotion_plan(**value)["missing_information"])


def test_confirmed_goal_is_reused_but_is_never_budget_approval(env):
    store = OperationGoalStore()
    value = goal_input()
    goal = store.save(GoalSaveRequest.model_validate(value))
    path = goal["goal_ref"]["artifact_path"]
    assert not plans.prepare_promotion_plan(metrics_artifact_path=str(env["path"]), goal_artifact_path=path)["success"]
    store.confirm(goal["goal_ref"], conversation_id=str(uuid4()), ui_artifact_id=str(uuid4()), idempotency_key=str(uuid4()))
    prepared = plans.prepare_promotion_plan(metrics_artifact_path=str(env["path"]), goal_artifact_path=path)
    assert prepared["success"], prepared
    assert prepared["business"]["company_name"] == value["company_name"]
    assert prepared["business"]["objective"] == value["operation_goal"]["summary"]
    assert prepared["business"]["budget_cap_cny"] is None
    goal_page = plans.load_promotion_plan(artifact_path=prepared["input_artifact_path"], section="goal")
    assert goal_page["success"] and goal_page["total"] > 0
    suggestion = proposal(prepared)
    suggestion["candidates"][0]["budget_cny"] = None
    data = saved_data(plans.save_promotion_plan(**suggestion))
    assert data["approval_status"] == data["budget"]["approval_status"] == "pending_approval"


def test_errors_and_saved_files_do_not_echo_configured_secrets(env, monkeypatch):
    secret = "synthetic-secret-for-test-123456"
    monkeypatch.setenv("TEST_API_KEY", secret)
    prepared = prepare(env, objective="测试 " + secret)
    assert secret not in json.dumps(prepared)
    text = Path(prepared["input_artifact_path"]).read_text("utf-8")
    assert secret not in text and "<REDACTED>" in text
    error = plans.save_promotion_plan(**proposal(prepared), approval_status=secret)
    assert secret not in json.dumps(error)


def test_agent_reads_evidence_and_saves_plan_via_registered_tools(env):
    prepared = prepare(env)
    value = proposal(prepared)
    client = FakeLLMClient([
        FakeMessage(None, [make_tool_call("load_promotion_plan", json.dumps({"artifact_path": prepared["input_artifact_path"], "section": "posts"}), call_id="posts")]),
        FakeMessage(None, [make_tool_call("save_promotion_plan", json.dumps(value), call_id="save")]),
        FakeMessage("计划已保存，待人工审批；市场研究与咨询数据仍待补充。"),
    ])
    agent = promotion_agent(client)
    answer = agent.run("根据指定输入生成投流计划", skill_name=SKILL_NAME)
    assert "待人工审批" in answer
    observed = [json.loads(message["content"]) for message in client.requests[-1] if message["role"] == "tool"]
    assert all(result["success"] for result in observed)
    assert Path(observed[-1]["report_path"]).exists()
    for name in plans.PROMOTION_TOOL_HANDLERS:
        assert name not in DEFAULT_TOOL_HANDLERS and name in TOOL_DEFINITIONS
        assert DEFAULT_TOOL_BUDGETS[name] > 0
        assert name in {tool["function"]["name"] for tool in client.tool_requests[0]}
    assert not any("approve" in name or "execute_promotion" in name for name in plans.PROMOTION_TOOL_HANDLERS)


def test_research_pagination_keeps_complete_evidence_and_topic_references(env):
    research = _valid_result()
    research["key_findings"] *= 8
    for finding in research["key_findings"]:
        finding["statement"] = "长文本证据" * 190
    result = ContentResearchTools(output_dir=content_research.CONTENT_RESEARCH_OUTPUT_DIR).save_result(**research)
    prepared = prepare(env, research_artifact_path=result["artifact_path"])
    offset, records = 0, []
    while True:
        page = plans.load_promotion_plan(artifact_path=prepared["input_artifact_path"], section="research", offset=offset)
        assert page["success"]
        assert len(json.dumps(page, ensure_ascii=False)) < 8000
        records.extend(page["items"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(records) == page["total"]
    assert any(item.get("group") == "candidate_topics" and item["source_refs"] == ["source-1"] for item in records)


def test_empty_metrics_prepare_but_no_invented_candidate(env):
    data = env["metrics"]
    data.update(posts=[], collected_count=0, source_total=0)
    env["path"].write_text(json.dumps(data), "utf-8")
    prepared = prepare(env)
    assert prepared["success"] and "posts" in prepared["missing_information"]
    assert not plans.save_promotion_plan(**proposal(prepared))["success"]


def test_long_candidate_is_readable_without_context_truncation(env):
    value = proposal(prepare(env))
    value["candidates"][0]["stop_conditions"] = [{"condition": "测试边界描述" * 240, "required_data": ["spend"]} for _ in range(5)]
    result = plans.save_promotion_plan(**value)
    data = saved_data(result)
    offset, fragments = 0, []
    while True:
        page = plans.load_promotion_plan(artifact_path=result["artifact_path"], section="candidates", offset=offset)
        assert page["success"] and len(json.dumps(page, ensure_ascii=False)) < 8000
        fragments.extend(page["items"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert all(fragment["format"] == "json_fragment" for fragment in fragments)
    assert json.loads("".join(fragment["text"] for fragment in fragments)) == data["candidates"][0]


def test_research_cannot_silently_use_a_different_goal_version(env):
    store = OperationGoalStore()
    goal = store.save(GoalSaveRequest.model_validate(goal_input()))
    store.confirm(goal["goal_ref"], conversation_id=str(uuid4()), ui_artifact_id=str(uuid4()), idempotency_key=str(uuid4()))
    research = _valid_result()
    for key in ("company_name", "company_profile", "operation_goal"):
        research.pop(key)
    research["goal_artifact_path"] = goal["goal_ref"]["artifact_path"]
    artifact = ContentResearchTools(output_dir=content_research.CONTENT_RESEARCH_OUTPUT_DIR).save_result(**research)
    assert artifact["success"]
    newer = store.save(GoalSaveRequest(**goal_input(), previous_goal_artifact_path=goal["goal_ref"]["artifact_path"]))
    store.confirm(newer["goal_ref"], conversation_id=str(uuid4()), ui_artifact_id=str(uuid4()), idempotency_key=str(uuid4()))
    result = plans.prepare_promotion_plan(metrics_artifact_path=str(env["path"]),
        goal_artifact_path=newer["goal_ref"]["artifact_path"], research_artifact_path=artifact["artifact_path"])
    assert not result["success"] and "目标版本" in result["message"]


def test_agent_prepares_reads_and_saves_using_actual_tool_outputs(env):
    class WorkflowClient:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools=None):
            self.step += 1
            if self.step == 1:
                name, arguments = "prepare_promotion_plan", {"metrics_artifact_path": str(env["path"])}
            elif self.step == 2:
                prepared = json.loads(messages[-1]["content"])
                assert prepared["success"]
                self.prepared = prepared
                name, arguments = "load_promotion_plan", {"artifact_path": prepared["input_artifact_path"], "section": "posts"}
            elif self.step == 3:
                page = json.loads(messages[-1]["content"])
                assert page["items"][0]["views"] == 100
                name, arguments = "save_promotion_plan", proposal(self.prepared)
                arguments["candidates"][0].update(budget_cny=None, post_url=None)
            else:
                result = json.loads(messages[-1]["content"])
                assert result["success"] and result["readiness"] == "needs_information"
                assert Path(result["report_path"]).exists()
                return FakeMessage("已保存待补充的投流计划，未执行投放。")
            return FakeMessage(None, [make_tool_call(name, json.dumps(arguments), call_id=str(self.step))])

    client = WorkflowClient()
    agent = promotion_agent(client)
    assert "已保存" in agent.run("用指定数据生成投流计划", skill_name=SKILL_NAME)
    assert client.step == 4


def test_no_path_collects_once_and_freezes_returned_file(env, monkeypatch):
    calls = []
    monkeypatch.setattr(plans, "REPOSITORY_ROOT", env["tmp"])
    def collect():
        calls.append(True)
        return {"success": True, "path": str(env["path"].relative_to(env["tmp"])), "job_id": "fresh-job"}
    monkeypatch.setattr(plans, "collect_xiaohongshu_metrics", collect)
    result = plans.prepare_promotion_plan(objective="增加观看", primary_metric="views")
    assert result["success"] and calls == [True]
    assert result["collection"] == {"mode": "fresh", "job_id": "fresh-job"}
    snapshot = json.loads(Path(result["input_artifact_path"]).read_text())
    assert snapshot["metrics"]["posts"] == env["metrics"]["posts"]
    assert snapshot["business"]["budget_cap_cny"] is None
    assert "metrics_artifact_path" not in snapshot["business"]
    # 刷新后的原文件不影响本次已冻结的证据。
    changed = deepcopy(env["metrics"])
    changed["posts"][0]["views"] = 999
    env["path"].write_text(json.dumps(changed))
    loaded = plans.load_promotion_plan(artifact_path=result["input_artifact_path"], section="posts")
    assert loaded["items"][0]["views"] == 100


@pytest.mark.parametrize("code,status", [("login_required", "needs_user_action"),
    ("account_mismatch", "needs_user_action"), ("RPA_TIMEOUT", "failed"),
    ("RPA_ACCOUNT_NOT_BOUND", "needs_user_action")])
def test_collection_failure_preserves_action_and_does_not_use_old_file(env, monkeypatch, code, status):
    calls = []
    failure = {"success": False, "error_type": code, "status": status,
               "message": "请处理专用Chrome后重试", "retryable": False,
               "needs_user_action": status == "needs_user_action", "job_id": "failed-job"}
    def collect():
        calls.append(True)
        return failure
    monkeypatch.setattr(plans, "collect_xiaohongshu_metrics", collect)
    result = plans.prepare_promotion_plan()
    assert result == {**failure, "stage": "collect_metrics"}
    assert calls == [True]
    assert env["path"].is_file()  # 虽有旧文件，也不能静默使用它。
    assert not plans.PROMOTION_DIR.exists()


def test_invalid_business_input_fails_before_collection(env):
    assert not plans.prepare_promotion_plan(budget_cap_cny="-1")["success"]
    assert not plans.prepare_promotion_plan(goal_artifact_path="/missing/goal.json")["success"]


def test_existing_file_and_revision_reuse_skip_collection(env):
    prepared = prepare(env)
    assert prepared["collection"] == {"mode": "existing"}
    first = plans.save_promotion_plan(**proposal(prepared))
    overview = plans.load_promotion_plan(artifact_path=first["artifact_path"])
    assert overview["input_artifact_path"] == prepared["input_artifact_path"]
    second = plans.save_promotion_plan(**proposal(overview,
        previous_plan_artifact_path=first["artifact_path"], revision_reason="复用证据修改说明"))
    assert second["success"] and second["version"] == 2
    assert Path(first["artifact_path"]).is_file()


def test_one_sentence_collects_reads_and_saves(env, monkeypatch):
    calls = []
    def collect():
        calls.append(True)
        return {"success": True, "path": str(env["path"]), "job_id": "fresh-job"}
    monkeypatch.setattr(plans, "collect_xiaohongshu_metrics", collect)
    class WorkflowClient:
        def complete(self, messages, tools=None):
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if not results:
                assert "生成投流方案" in messages[-1]["content"]
                name, args = "prepare_promotion_plan", {}
            elif len(results) == 1:
                self.prepared = results[-1]
                assert self.prepared["collection"]["mode"] == "fresh"
                name, args = "load_promotion_plan", {"artifact_path": self.prepared["input_artifact_path"], "section": "posts"}
            elif len(results) == 2:
                assert results[-1]["items"][0]["views"] == 100
                name, args = "save_promotion_plan", proposal(self.prepared)
                args["candidates"][0].update(budget_cny=None, post_url=None)
            else:
                assert results[-1]["success"] and Path(results[-1]["report_path"]).is_file()
                return FakeMessage("已根据本次采集数据保存待审批方案")
            return FakeMessage(None, [make_tool_call(name, json.dumps(args), call_id=str(len(results)))])
    agent = promotion_agent(WorkflowClient())
    assert "已根据本次采集" in agent.run("生成投流方案")
    assert calls == [True]
    assert [x["tool_name"] for x in agent.last_tool_results] == ["prepare_promotion_plan", "load_promotion_plan", "save_promotion_plan"]


def test_agent_cannot_repeat_collection_after_failure(env, monkeypatch):
    calls = []
    def collect():
        calls.append(True)
        return {"success": False, "error_type": "login_required", "needs_user_action": True,
                "retryable": False, "message": "请登录专用Chrome"}
    monkeypatch.setattr(plans, "collect_xiaohongshu_metrics", collect)
    client = FakeLLMClient([FakeMessage(None, [make_tool_call("prepare_promotion_plan", "{}", call_id="first")]),
        FakeMessage(None, [make_tool_call("prepare_promotion_plan", json.dumps({"metrics_artifact_path": str(env["path"])}), call_id="fallback")]),
        FakeMessage("请登录后重试")])
    agent = promotion_agent(client)
    agent.run("生成投流方案")
    assert calls == [True]
    assert json.loads(client.requests[-1][-1]["content"])["error_type"] == "TOOL_BUDGET_EXCEEDED"
    assert agent.last_tool_results == []
    assert not plans.PROMOTION_DIR.exists()
