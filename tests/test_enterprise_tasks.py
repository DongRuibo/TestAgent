"""企业档案、自动路由与当前任务区：全部使用合成数据，不访问模型或 RAG。"""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent.skills import SkillCatalog, SkillConfigurationError
from test_operation_goal_handoff import goal_input
from testagent_dev_agent.paths import REPOSITORY_ROOT
from tools import operation_goals
from tools.operation_goals import OperationGoalStore
from tools.operation_goals_ui import build_operation_goal_ui
from web_agent import skill_routing
from web_agent.app import create_app
from web_agent.conversations import SQLiteConversationStore
from web_agent.enterprise import EnterpriseStore


class DocumentGateway:
    def __init__(self):
        self.document_id = str(uuid4())
        self.status = "READY"
        self.calls = []
        self.closed = 0

    def close(self):
        self.closed += 1

    def list_documents(self, **kwargs):
        return {"success": True, "documents": [{"document_id": self.document_id,
            "filename": "合成企业.md", "status": self.status}]}

    def search(self, query, *, document_ids):
        self.calls.append(document_ids)
        return {"success": True, "candidates": [
            {"document_id": self.document_id, "document_name": "合成企业.md", "content": "合成服务区域：杭州"},
            {"document_id": str(uuid4()), "document_name": "其他文档", "content": "不得进入返回结果"},
        ]}


@pytest.fixture
def task_env(tmp_path, monkeypatch):
    monkeypatch.setattr(operation_goals, "OPERATION_GOAL_DIR", tmp_path / "goals")
    monkeypatch.setattr(skill_routing, "recommend_skill", lambda *args, **kwargs: SimpleNamespace(skill_name=None, reason="普通问答"))
    store = SQLiteConversationStore(tmp_path / "tasks.sqlite3")
    goals = OperationGoalStore()
    gateway = DocumentGateway()
    tracker = SimpleNamespace(mode="ask", calls=[], results=[])

    class FakeAgent:
        def __init__(self):
            self.tool_handlers = goals.handlers()
            self.generation_ui_artifacts = []

        def run(self, message, *, history, episode_id, skill_name=None, auto_select_skill=True, business_context=None):
            assert auto_select_skill is False
            tracker.calls.append({"message": message, "context": business_context, "skill": skill_name})
            if tracker.mode == "error":
                raise RuntimeError("合成中断")
            if tracker.mode == "search":
                tracker.results.append(self.tool_handlers["search_knowledge_base"]("服务区域"))
            if tracker.mode == "save":
                value = goal_input()
                value.update(company_name="模型自行更换的企业", company_profile="不应覆盖确认档案")
                result = self.tool_handlers["save_operation_goal"](**value)
                assert result["success"], result
                tracker.results.append(result)
                self.generation_ui_artifacts.append(build_operation_goal_ui(result))
                return "目标已保存，请在对话卡片中确认。"
            return "希望制定哪个月份的目标？"

    def app():
        return create_app(lambda *args: FakeAgent(), conversation_store=store,
            skill_router=skill_routing.route_skill, knowledge_base_gateway_factory=lambda: gateway)

    return SimpleNamespace(client=TestClient(app()), app=app, store=store, tracker=tracker, gateway=gateway)


def save_profile(env, version=0, **extra):
    return env.client.put("/api/enterprise", json={"expected_version": version, "company_name": "合成公司",
        "company_profile": "杭州家居服务，只用于离线测试。", "confirmed": True, **extra})


def chat(env, **values):
    response = env.client.post("/api/chat", json={"message": "请制定小红书运营目标", **values})
    assert response.status_code == 200, response.text
    return response.json()


def test_confirmed_profile_is_versioned_and_survives_restart(task_env):
    env = task_env
    assert env.client.get("/api/enterprise").json() == {"profile": None}
    assert save_profile(env, confirmed=False).status_code == 409
    first = save_profile(env).json()["profile"]
    assert first["version"] == 1 and first["content_hash"]
    assert save_profile(env).status_code == 409
    second = save_profile(env, 1, company_name="合成新名称").json()["profile"]
    assert second["version"] == 2 and second["content_hash"] != first["content_hash"]
    assert TestClient(env.app()).get("/api/enterprise").json()["profile"] == second
    assert env.client.get("/enterprise").status_code == 200
    assert env.tracker.calls == []


def test_task_snapshot_reused_while_new_conversation_gets_new_profile(task_env):
    env = task_env
    save_profile(env)
    first = chat(env)
    identifier = first["conversation_id"]
    state = env.client.get(f"/api/conversations/{identifier}/current-task").json()
    assert state["phase"] == "goal_input"
    assert state["question"] == first["answer"]
    save_profile(env, 1, company_name="另一版名称")
    chat(env, conversation_id=identifier, message="9 月，优先增加有效咨询", task_reply=True)
    assert env.tracker.calls[-1]["skill"] == "operation-goal-setting"
    assert env.tracker.calls[-1]["context"]["enterprise_profile"]["version"] == 1
    chat(env, conversation_id=identifier, message="解释企业基本资料")
    assert env.tracker.calls[-1]["context"]["enterprise_profile"]["version"] == 1
    chat(env)
    assert env.tracker.calls[-1]["context"]["enterprise_profile"]["version"] == 2


def test_goal_save_binds_enterprise_and_revision_then_current_card_confirms(task_env):
    env = task_env
    save_profile(env)
    env.tracker.mode = "save"
    first = chat(env)
    identifier = first["conversation_id"]
    route = f"/api/conversations/{identifier}/current-task"
    state = env.client.get(route).json()
    assert state["phase"] == "goal_review"
    assert env.tracker.results[-1]["goal_snapshot"]["company_name"] == "合成公司"
    env.tracker.mode = "ask"
    chat(env, conversation_id=identifier, message="修改一下", task_reply=True)
    assert env.client.get(route).json()["phase"] == "goal_input"  # 不能被旧卡片覆盖
    env.tracker.mode = "save"
    chat(env, conversation_id=identifier, message="修订后的指标如下", task_reply=True)
    artifact = env.client.get(route).json()["artifact"]
    assert artifact["data"]["goal_ref"]["version"] == 2
    assert artifact["data"]["goal_ref"]["goal_id"] == state["artifact"]["data"]["goal_ref"]["goal_id"]
    event = {"conversation_id": identifier, "message_id": artifact["message_id"], "artifact_id": artifact["artifact_id"],
        "scenario": artifact["scenario"], "event_type": "confirm", "action_id": "confirm_operation_goal",
        "arguments": {}, "idempotency_key": str(uuid4())}
    assert env.client.post("/api/generation/event", json=event).status_code == 200
    assert env.client.get(route).json()["phase"] == "goal_ready"


def test_plain_chat_uses_default_profile_but_does_not_start_business_task(task_env):
    env = task_env
    save_profile(env)
    response = chat(env, message="介绍 operation-goal-setting 的用途")
    assert env.tracker.calls[-1]["skill"] is None
    assert env.tracker.calls[-1]["context"]["enterprise_profile"]["company_name"] == "合成公司"
    assert env.client.get(f"/api/conversations/{response['conversation_id']}/current-task").json()["phase"] == "idle"


def test_research_skill_text_and_explicit_api_cannot_bypass_paid_confirmation(task_env):
    env = task_env
    for values in ({"message": "$network-research-and-topic-selection 开始研究"},
                   {"skill_name": "network-research-and-topic-selection"}):
        response = chat(env, **values)
        assert "未启动新的付费取数" in response["answer"]
    assert env.tracker.calls == []


def test_no_profile_still_receives_goal_task_contract_and_failure_is_resumable(task_env):
    env = task_env
    first = chat(env)
    assert env.tracker.calls[-1]["context"]["task"] == "goal"
    assert env.tracker.calls[-1]["context"]["enterprise_profile"] is None
    env.tracker.mode = "error"
    identifier = first["conversation_id"]
    response = env.client.post("/api/chat", json={"conversation_id": identifier, "message": "9 月", "task_reply": True})
    assert response.status_code == 502
    assert env.client.get(f"/api/conversations/{identifier}/current-task").json()["phase"] == "goal_failed"
    assert env.client.get(f"/api/conversations/{identifier}/current-task").json()["draft"] == "9 月"
    env.tracker.mode = "ask"
    chat(env, conversation_id=identifier, message="继续", task_reply=True)
    assert env.client.get(f"/api/conversations/{identifier}/current-task").json()["phase"] == "goal_input"


def test_document_draft_does_not_confirm_profile_and_filters_scope(task_env):
    env = task_env
    ids = [env.gateway.document_id]
    response = env.client.post("/api/enterprise/document-draft", json={"document_ids": ids})
    assert response.status_code == 200 and response.json()["partial"] is True
    assert "合成服务区域" in response.json()["draft"]
    assert "不得进入返回结果" not in response.text
    assert env.client.get("/api/enterprise").json()["profile"] is None
    assert save_profile(env, document_ids=ids).status_code == 200
    env.tracker.mode = "search"
    chat(env)
    assert len(env.tracker.results[-1]["candidates"]) == 1
    assert env.gateway.calls[-1] == tuple(ids)
    assert env.gateway.closed == 3
    env.gateway.status = "PROCESSING"
    assert save_profile(env, 1, document_ids=ids).status_code == 409
    assert env.client.post("/api/enterprise/document-draft", json={"document_ids": ids}).status_code == 409
    assert save_profile(env, 1, document_ids=[str(uuid4())]).status_code == 409
    assert save_profile(env, 1, document_ids=[]).status_code == 200  # 可解除失效关联


def test_explicit_names_explanations_and_invalid_continuations():
    catalog = SkillCatalog.from_directory(REPOSITORY_ROOT / ".agents/skills")
    for message in ("$operation-goal-setting 制定目标", "使用 operation-goal-setting 制定目标并解释原因",
                    "【模拟业务】使用 $operation-goal-setting。制定月周目标并解释关系"):
        assert skill_routing.route_skill(message, catalog)[0] == "operation-goal-setting"
    assert skill_routing.route_skill("介绍 $operation-goal-setting 怎么使用", catalog)[0] is None
    with pytest.raises(SkillConfigurationError):
        skill_routing.route_skill("使用 unknown-skill 执行", catalog)
    with pytest.raises(SkillConfigurationError):
        skill_routing.route_skill("确认", catalog, task_reply=True)


def test_colloquial_combined_request_starts_with_goal_and_material_reply_continues_weekly():
    catalog = SkillCatalog.from_directory(REPOSITORY_ROOT / ".agents/skills")
    assert skill_routing.route_skill("帮我定一下这个月的小红书目标，顺便安排下周。这次先用模拟数据试试。", catalog)[0] == "operation-goal-setting"
    assert skill_routing.route_skill("小周能提供衣柜照片，下周二交", catalog,
        task={"phase": "goal_ready", "weekly_input_pending": True})[0] == "enterprise-weekly-operation-plan"
    assert skill_routing.route_skill("就三张现有照片，不补拍，也没有尺寸图。按这三张来改，别再列六种照片了。", catalog,
        task={"phase": "goal_ready", "weekly_plan_active": True})[0] == "enterprise-weekly-operation-plan"


def test_chat_goal_replies_continue_but_explanations_do_not_mutate(task_env):
    env = task_env
    first = chat(env)
    identifier = first["conversation_id"]
    chat(env, conversation_id=identifier, message="2026 年 9 月，希望多一些到店。", task_reply=True)
    assert env.tracker.calls[-1]["skill"] == "operation-goal-setting"
    for message in ("这个指标是什么？", "研究需要多少费用？"):
        chat(env, conversation_id=identifier, message=message, task_reply=True)
        assert env.tracker.calls[-1]["skill"] is None
    assert env.client.get(f"/api/conversations/{identifier}/current-task").json()["phase"] == "goal_input"


def test_restart_recovers_orphaned_goal_without_model_call(task_env):
    env = task_env
    first = chat(env)
    identifier = first["conversation_id"]
    tasks = EnterpriseStore(env.store)
    current = tasks.task(identifier)
    tasks.update_task(identifier, **{**current, "phase": "goal_running"})
    assert env.client.get(f"/api/conversations/{identifier}/current-task").json()["phase"] == "goal_running"
    restarted = TestClient(env.app())
    response = restarted.get(f"/api/conversations/{identifier}/current-task")
    assert response.status_code == 200
    assert response.json()["phase"] == "goal_failed"
    assert response.json()["draft"] == "请制定小红书运营目标"
    assert len(env.tracker.calls) == 1


def test_model_can_recommend_only_for_this_request(monkeypatch):
    catalog = SkillCatalog.from_directory(REPOSITORY_ROOT / ".agents/skills")
    calls = []
    def recommend(message, skills, *, use_rules):
        assert use_rules is False
        calls.append(message)
        return SimpleNamespace(skill_name="operation-goal-setting", reason="需要先定义运营目标")
    monkeypatch.setattr(skill_routing, "recommend_skill", recommend)
    assert skill_routing.route_skill("想给门店设定下个月的获客目标", catalog)[0] == "operation-goal-setting"
    assert calls == ["想给门店设定下个月的获客目标"]


def test_cost_question_with_skill_alias_uses_semantics_not_alias_activation(monkeypatch):
    catalog = SkillCatalog.from_directory(REPOSITORY_ROOT / ".agents/skills")
    seen = []
    def classify(message, skills, *, use_rules):
        assert not use_rules
        seen.append(message)
        return SimpleNamespace(skill_name=None, reason="这是费用询问")
    monkeypatch.setattr(skill_routing, "recommend_skill", classify)
    for message in ("小红书研究选题多少钱？", "制定小红书运营目标需要多少费用？"):
        assert skill_routing.route_skill(message, catalog)[0] is None
    assert len(seen) == 2
