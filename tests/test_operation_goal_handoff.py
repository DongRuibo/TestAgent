"""14.1 用户确认 → 14.2 固定样本 → 14.3 来源链的离线验收。"""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from testagent_dev_agent.paths import REPOSITORY_ROOT
from tools import operation_goals, content_research
from tools.operation_goals import GoalSaveRequest, OperationGoalStore, GOAL_TOOL_NAMES
from tools.operation_goals_ui import build_operation_goal_ui
from tools.content_research import ContentResearchTools, load_content_research_artifact
from tools.content_operations import generate_content_weekly_plan
from web_agent import app as web_app
from web_agent.conversations import SQLiteConversationStore
from test_content_research import _valid_result


def goal_input():
    return json.loads((REPOSITORY_ROOT / "fixtures/content_operations_v0/operation_goal.json").read_text("utf-8"))


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(operation_goals, "OPERATION_GOAL_DIR", tmp_path / "goals")
    monkeypatch.setattr(content_research, "CONTENT_RESEARCH_OUTPUT_DIR", tmp_path / "research")
    return OperationGoalStore(), ContentResearchTools(output_dir=tmp_path / "research")


def confirmation_client(tmp_path, handoff):
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.create()
    ui = build_operation_goal_ui(handoff)
    message_id = store.append_assistant_response(conversation_id, "待确认目标", ui_artifacts=[ui])
    client = TestClient(web_app.create_app(
        lambda *args: pytest.fail("confirmation must not run Agent"), conversation_store=store,
    ))
    event = {
        "conversation_id": conversation_id, "message_id": message_id,
        "artifact_id": ui["artifact_id"], "scenario": ui["scenario"],
        "event_type": "confirm", "action_id": "confirm_operation_goal",
        "arguments": {}, "idempotency_key": str(uuid4()),
    }
    return client, event


def research_input(path):
    value = _valid_result()
    for name in ("company_name", "company_profile", "operation_goal"):
        value.pop(name)
    return {**value, "goal_artifact_path": path}


def confirmed_goal(tmp_path, stores):
    goals, _ = stores
    handoff = goals.save(GoalSaveRequest.model_validate(goal_input()))
    client, event = confirmation_client(tmp_path, handoff)
    response = client.post("/api/generation/event", json=event)
    assert response.status_code == 200, response.text
    return handoff, client, event


def test_goal_to_research_to_weekly_plan_and_confirmation_replay(tmp_path, stores):
    goals, research = stores
    handoff = goals.save(GoalSaveRequest.model_validate(goal_input()))
    path = handoff["goal_ref"]["artifact_path"]
    assert research.save_result(**research_input(path))["success"] is False
    assert not Path(path).with_name("confirmation.json").exists()
    client, event = confirmation_client(tmp_path, handoff)
    response = client.post("/api/generation/event", json=event)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed_in_conversation"
    assert client.post("/api/generation/event", json=event).json() == response.json()
    loaded = goals.load(path, require_confirmed=True)
    confirmation = loaded["goal_snapshot"]["confirmation"]
    assert confirmation["source"] == "web_user_action"
    assert confirmation["ui_artifact_id"] == event["artifact_id"]
    restored = client.get(f"/api/conversations/{event['conversation_id']}").json()
    assert not restored["messages"][0].get("artifacts")
    assert "历史目标记录" in restored["messages"][0]["content"]
    assert "此版本已确认" in restored["messages"][0]["content"]
    saved = research.save_result(**research_input(path))
    assert saved["success"] is True, saved
    plan = generate_content_weekly_plan("2026-09-07", research_artifact_path=saved["artifact_path"])
    assert plan["goal_ref"] == handoff["goal_ref"]
    assert plan["goal_snapshot"] == loaded["goal_snapshot"]
    assert plan["goal_snapshot"]["operation_goal"]["metrics"][0]["baseline"] is None
    assert plan["research_execution_status"] == "partial_success"
    assert plan["research_limitations"] == _valid_result()["limitations"]
    assert plan["research_approval_status"] == "pending_confirmation"
    assert plan["approval_status"] == "pending_confirmation"
    assert plan["research_ref"]["research_id"] == saved["research_id"]
    assert plan["company_profile"] == goal_input()["company_profile"]


def test_goal_revision_resets_confirmation_but_preserves_historical_research(tmp_path, stores):
    goals, research = stores
    handoff, client, event = confirmed_goal(tmp_path, stores)
    path = handoff["goal_ref"]["artifact_path"]
    saved = research.save_result(**research_input(path))
    changed = goal_input()
    changed["operation_goal"]["summary"] = "修改后的试运行目标"
    request = GoalSaveRequest(**changed, previous_goal_artifact_path=path)
    newer = goals.save(request)
    assert newer["goal_ref"]["goal_id"] == handoff["goal_ref"]["goal_id"]
    assert newer["goal_ref"]["version"] == 2
    assert newer["goal_snapshot"]["confirmation"]["status"] == "pending_confirmation"
    assert research.save_result(**research_input(path))["success"] is False
    assert research.save_result(**research_input(newer["goal_ref"]["artifact_path"]))["success"] is False
    with pytest.raises(ValueError, match="新版本"):
        goals.save(request)
    event["idempotency_key"] = str(uuid4())
    assert client.post("/api/generation/event", json=event).status_code == 409
    plan = generate_content_weekly_plan("2026-09-07", research_artifact_path=saved["artifact_path"])
    assert plan["goal_snapshot"]["version"] == 1
    assert plan["weekly_goal"] == goal_input()["operation_goal"]["summary"]


@pytest.mark.parametrize("extra", [
    {"confirmation": {"status": "confirmed_in_conversation"}}, {"version": 9},
    {"goal_id": str(uuid4())}, {"content_hash": "a" * 64},
])
def test_model_cannot_supply_goal_identity_or_confirmation(stores, extra):
    goals, _ = stores
    assert goals.save_tool(**goal_input(), **extra)["success"] is False
    assert set(goals.handlers()) == set(GOAL_TOOL_NAMES)
    assert "confirm_operation_goal" not in goals.handlers()
    assert not goals.root.exists()


def test_goal_schema_preserves_platform_and_period_boundaries():
    value = goal_input()
    value["operation_goal"]["period"]["end"] = "2026-08-01"
    with pytest.raises(ValidationError):
        GoalSaveRequest.model_validate(value)
    value = goal_input()
    value["operation_goal"]["platform"] = "douyin"
    with pytest.raises(ValidationError):
        GoalSaveRequest.model_validate(value)


def test_model_cannot_supply_research_handoff_or_leak_rejected_input(stores):
    _, research = stores
    result = research.save_result(**_valid_result(), goal_snapshot={"secret": "test-secret-value"})
    assert result["success"] is False
    assert "test-secret-value" not in result["message"]
    assert not research.output_dir.exists()


@pytest.mark.parametrize("corruption", ["hash", "symlink", "outside", "receipt", "oversize"])
def test_goal_files_are_validated(tmp_path, stores, corruption):
    goals, _ = stores
    handoff = goals.save(GoalSaveRequest(**goal_input()))
    path = Path(handoff["goal_ref"]["artifact_path"])
    if corruption == "hash":
        value = json.loads(path.read_text("utf-8"))
        value["company_name"] = "另一家公司"
        path.write_text(json.dumps(value), "utf-8")
    elif corruption == "symlink":
        link = goals.root / "linked"
        link.symlink_to(path.parent, target_is_directory=True)
        path = link / "goal.json"
    elif corruption == "outside":
        outside = tmp_path / "goal.json"
        outside.write_text(path.read_text("utf-8"), "utf-8")
        path = outside
    elif corruption == "receipt":
        path.with_name("confirmation.json").write_text("[]", "utf-8")
    else:
        path.write_text(" " * (operation_goals.MAX_GOAL_BYTES + 1), "utf-8")
    with pytest.raises(ValueError):
        goals.load(str(path))


@pytest.mark.parametrize("corruption", ["snapshot", "reference", "company", "downgrade"])
def test_research_rejects_changed_goal_lineage(tmp_path, stores, corruption):
    _, research = stores
    handoff, _, _ = confirmed_goal(tmp_path, stores)
    path = handoff["goal_ref"]["artifact_path"]
    conflicting = research_input(path)
    conflicting["company_name"] = "另一家公司"
    assert research.save_result(**conflicting)["success"] is False
    saved = research.save_result(**research_input(path))
    file = Path(saved["artifact_path"])
    value = json.loads(file.read_text("utf-8"))
    if corruption == "snapshot":
        value["goal_snapshot"]["operation_goal"]["summary"] = "替换目标"
    elif corruption == "reference":
        value["goal_ref"]["content_hash"] = "f" * 64
    elif corruption == "company":
        value["company_name"] = "另一家公司"
    else:
        value["schema_version"] = "1.0"
    file.write_text(json.dumps(value), "utf-8")
    with pytest.raises(ValueError):
        load_content_research_artifact(str(file))


def test_true_legacy_artifact_keeps_no_claim_of_upstream_confirmation(stores):
    _, research = stores
    saved = research.save_result(**_valid_result())
    path = Path(saved["artifact_path"])
    value = json.loads(path.read_text("utf-8"))
    value["schema_version"] = "1.0"
    for key in ("input_mode", "goal_ref", "goal_snapshot", "version"):
        value.pop(key)
    path.write_text(json.dumps(value), "utf-8")
    plan = generate_content_weekly_plan("2026-09-07", research_artifact_path=str(path))
    assert plan["research_input_mode"] == "legacy"
    assert plan["goal_ref"] is None and plan["goal_snapshot"] is None


def test_confirmation_is_bound_to_stored_card_and_conversation(tmp_path, stores):
    goals, _ = stores
    handoff = goals.save(GoalSaveRequest(**goal_input()))
    client, event = confirmation_client(tmp_path, handoff)
    assert client.post("/api/generation/event", json={**event, "conversation_id": str(uuid4())}).status_code == 404
    assert client.post("/api/generation/event", json={**event, "message_id": event["message_id"] + 1}).status_code == 409
    assert client.post("/api/generation/event", json={**event, "arguments": {"goal_ref": handoff["goal_ref"]}}).status_code == 422
    assert goals.load(handoff["goal_ref"]["artifact_path"])["goal_snapshot"]["confirmation"]["status"] == "pending_confirmation"


def test_default_web_factory_presents_goal_and_limits_tools(stores, monkeypatch):
    monkeypatch.setattr(web_app.LLMClient, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(web_app, "build_memory_client_from_env", lambda: None)
    agent = web_app._default_agent_factory(lambda text: None, web_app.AttachmentRetrievalScope(), mcp_tools=None)
    result = agent.tool_handlers["save_operation_goal"](**goal_input())
    assert result["success"] is True
    assert len(agent.generation_ui_artifacts) == 1
    assert agent.generation_ui_artifacts[0]["data"]["goal_ref"] == result["goal_ref"]
    goal_skill = agent.skill_catalog.resolve("", explicit_name="operation-goal-setting")
    assert "save_operation_goal" in goal_skill.allowed_tools
    assert "social_search_posts" not in goal_skill.allowed_tools
    assert "confirm_operation_goal" not in agent.tool_handlers


def combined_goal_input():
    from copy import deepcopy
    value = goal_input()
    value['weekly_goal'] = {**deepcopy(value['operation_goal']),
        'contribution': '先验证咨询来源，再根据记录调整月目标。'}
    value['operation_goal']['period'] = {'start': '2026-09-01', 'end': '2026-09-30'}
    value['operation_goal']['summary'] = '整月建立稳定的咨询来源记录。'
    return value


def test_combined_goal_one_save_confirmation_research_and_revision(tmp_path, stores):
    from web_agent.business_text import goal_text
    goals, research = stores
    value = combined_goal_input()
    handoff = goals.save(GoalSaveRequest(**value))
    path = handoff['goal_ref']['artifact_path']
    rendered = goal_text(handoff)
    assert value['operation_goal']['summary'] in rendered
    assert value['weekly_goal']['summary'] in rendered
    assert '确认范围包括以上月目标和简单周目标' in rendered
    assert research.save_result(**research_input(path))['success'] is False
    client, event = confirmation_client(tmp_path, handoff)
    assert client.post('/api/generation/event', json=event).status_code == 200
    saved = research.save_result(**research_input(path))
    assert saved['success'], saved
    result = load_content_research_artifact(saved['artifact_path'])
    assert result['operation_goal'] == value['weekly_goal']['summary']
    assert result['goal_snapshot']['operation_goal'] == value['operation_goal']
    assert result['goal_snapshot']['weekly_goal'] == value['weekly_goal']
    value['weekly_goal']['summary'] = '修订周重点，月目标保持不变。'
    revised = goals.save(GoalSaveRequest(**value, previous_goal_artifact_path=path))
    assert revised['goal_ref']['goal_id'] == handoff['goal_ref']['goal_id']
    assert revised['goal_ref']['version'] == 2
    assert revised['goal_snapshot']['confirmation']['status'] == 'pending_confirmation'
    assert not research.save_result(**research_input(revised['goal_ref']['artifact_path']))['success']
    assert load_content_research_artifact(saved['artifact_path'])['goal_snapshot']['weekly_goal']['summary'] != value['weekly_goal']['summary']


@pytest.mark.parametrize('period', [
    {'start': '2026-08-31', 'end': '2026-09-06'},
    {'start': '2026-09-28', 'end': '2026-10-04'},
    {'start': '2026-09-07', 'end': '2026-09-14'},
])
def test_combined_goal_rejects_outside_month_or_long_week(period):
    value = combined_goal_input()
    value['weekly_goal']['period'] = period
    with pytest.raises(ValidationError):
        GoalSaveRequest(**value)


def test_combined_goal_rejects_more_than_three_week_metrics():
    value = combined_goal_input()
    value['weekly_goal']['metrics'] *= 4
    with pytest.raises(ValidationError):
        GoalSaveRequest(**value)


def test_goal_save_can_correct_error_but_only_succeed_once(stores):
    from copy import deepcopy
    from agent.loop import AgentLoop, TOOL_DEFINITIONS
    goals, _ = stores
    name = 'save_operation_goal'
    agent = AgentLoop(client=object(), trace=None,
        tool_definitions={name: TOOL_DEFINITIONS[name]},
        tool_handlers={name: goals.save_tool}, tool_budgets={name: 2})
    valid = combined_goal_input()
    invalid = deepcopy(valid)
    invalid['weekly_goal']['contribution'] = ''
    usage, cache = {}, {}
    def save(value):
        return agent._execute_tool_with_policy(name, json.dumps(value), tool_usage=usage, result_cache=cache)
    failed = save(invalid)
    assert not failed['success']
    assert 'weekly_goal.contribution' in failed['message']
    saved = save(valid)
    assert saved['success'], saved
    assert save(valid)['goal_ref'] == saved['goal_ref']
    valid['weekly_goal']['summary'] = '另存一份应被拦截'
    assert save(valid)['error_type'] == 'GOAL_ALREADY_SAVED'
    assert len(list(goals.root.glob('*/v*/goal.json'))) == 1


def test_goal_tool_accepts_double_encoded_week_but_still_validates(stores):
    goals, _ = stores
    value = combined_goal_input()
    original = value['weekly_goal']
    value['weekly_goal'] = json.dumps(original, ensure_ascii=False)
    saved = goals.save_tool(**value)
    assert saved['success'], saved
    assert saved['goal_snapshot']['weekly_goal'] == original
    assert isinstance(json.loads(Path(saved['artifact_path']).read_text())['weekly_goal'], dict)
    original['period']['end'] = '2026-10-05'
    assert not goals.save_tool(**{**value, 'weekly_goal': json.dumps(original)})['success']
    for invalid in ('不是JSON', 'null', '[]', '{"summary":"缺少必填字段"}'):
        assert not goals.save_tool(**{**value, 'weekly_goal': invalid})['success']
    assert len(list(goals.root.glob('*/v*/goal.json'))) == 1
