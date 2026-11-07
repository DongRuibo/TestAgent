"""固定运营 Workflow 的离线状态机、API 和人工审核验收。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_operation_goal_handoff import goal_input, research_input
from test_content_research import synthetic_detail
from tools import content_research, operation_goals
from tools.content_research import ContentResearchTools
from tools.operation_goals import GoalSaveRequest, OperationGoalStore
from tools.operation_goals_ui import build_operation_goal_ui
from tools.content_topic_plan_ui import REVIEW_TOPIC_ACTION, TOPIC_PLAN_SCENARIO
from web_agent.app import create_app
from web_agent.content_workflow import ContentWorkflow, WorkflowAction
from web_agent.conversations import SQLiteConversationStore, GenerationUiActionConflictError


@pytest.fixture
def workflow_env(tmp_path, monkeypatch):
    monkeypatch.setattr(operation_goals, "OPERATION_GOAL_DIR", tmp_path / "goals")
    monkeypatch.setattr(content_research, "CONTENT_RESEARCH_OUTPUT_DIR", tmp_path / "research")
    goals = OperationGoalStore()
    research = ContentResearchTools(output_dir=tmp_path / "research")
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    identifier = store.create()
    goal = goals.save(GoalSaveRequest(**goal_input()))
    ui = build_operation_goal_ui(goal)
    message_id = store.append_assistant_response(identifier, "离线合成目标", ui_artifacts=[ui])
    tracker = SimpleNamespace(calls=0, mode="success", skill=None)

    class FakeAgent:
        def __init__(self):
            self.tool_handlers = research.handlers()
            self.tool_handlers["social_get_post_detail"] = synthetic_detail
            self.generation_ui_artifacts = []

        def run(self, task, *, history, episode_id, skill_name=None, auto_select_skill=True):
            tracker.calls += 1
            tracker.skill = skill_name
            tracker.completion_contract = getattr(self, "completion_contract", None)
            assert auto_select_skill is False
            assert history == []
            if tracker.mode == "before_save":
                raise RuntimeError("synthetic-secret-must-not-leak")
            if tracker.mode == "no_save":
                return "我说已经保存，但没有真正调用工具。"
            value = research_input(goal["goal_ref"]["artifact_path"])
            for source in value["source_references"]:
                self.tool_handlers["social_get_post_detail"](url=source["url"])
            if tracker.mode == "wrong_goal":
                value["goal_artifact_path"] = "/untrusted/goal.json"
            if tracker.mode == "failed_result":
                value.update(execution_status="failed", candidate_topics=[])
            if tracker.mode in {"invalid_save", "corrected_save"}:
                value["candidate_topics"][0]["why_fit_company"] = ["应当使用字符串"]
            result = self.tool_handlers["save_content_research_result"](**value)
            if tracker.mode == "corrected_save":
                assert result["success"] is False
                result = self.tool_handlers["save_content_research_result"](**research_input(goal["goal_ref"]["artifact_path"]))
            if tracker.mode == "after_save":
                raise RuntimeError("synthetic-secret-must-not-leak")
            return json.dumps(result, ensure_ascii=False)

    client = TestClient(create_app(lambda *args: FakeAgent(), conversation_store=store))
    event = {"conversation_id": identifier, "message_id": message_id, "artifact_id": ui["artifact_id"],
        "scenario": ui["scenario"], "event_type": "confirm", "action_id": "confirm_operation_goal",
        "arguments": {}, "idempotency_key": str(uuid4())}
    return SimpleNamespace(store=store, client=client, identifier=identifier, goal=goal, event=event,
        tracker=tracker, goals=goals, path=f"/api/conversations/{identifier}/content-workflow")


def act(env, action, **extra):
    current = env.client.get(env.path).json()["workflow"]
    return env.client.post(env.path, json={"action": action,
        "expected_revision": current["revision"] if current else 0, **extra})


def bind(env):
    assert env.client.post("/api/generation/event", json=env.event).status_code == 200
    response = act(env, "bind_goal", goal_artifact_id=env.event["artifact_id"])
    assert response.status_code == 200, response.text
    return response.json()


def start(env):
    bind(env)
    return act(env, "research", confirm_research=True)


@pytest.mark.parametrize("mode", ["no_save", "failed_result", "success"])
def test_research_completion_uses_bound_artifact_and_source_status(workflow_env, mode):
    from agent.completion import complete_answer
    env = workflow_env
    env.tracker.mode = mode
    start(env)
    assert env.tracker.completion_contract is not None
    answer, receipt = complete_answer("模型认为研究已经完成", env.tracker.completion_contract)
    current = env.store.content_workflow(env.identifier)
    expected = current.get("research_status") == "success" and bool(current.get("research_path"))
    assert json.loads(answer)["completed"] is expected
    if mode in {"no_save", "failed_result"}:
        assert receipt["status"] == "needs_review"


@pytest.mark.parametrize("code", ["PROVIDER_TIMEOUT", "PROVIDER_RESULT_PENDING"])
def test_uncertain_provider_result_stops_all_further_fetches(workflow_env, code):
    env = workflow_env
    bind(env)
    calls = []
    results = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return {"success": False, "error_type": code, "retryable": True}
    def run(identifier, goal_path, configure):
        agent = SimpleNamespace(tool_handlers={"social_search_posts": fetch, "social_get_comments": fetch,
            "save_content_research_result": lambda **kwargs: {"success": False}})
        configure(agent)
        results.append(agent.tool_handlers["social_search_posts"](keyword="模拟收纳"))
        results.append(agent.tool_handlers["social_search_posts"](keyword="换词也不能重取"))
        results.append(agent.tool_handlers["social_get_comments"](url="https://www.xiaohongshu.com/explore/test"))
    workflow = ContentWorkflow(env.store, run)
    state = env.store.content_workflow(env.identifier)
    result = workflow.act(env.identifier, WorkflowAction(action="research", expected_revision=state["revision"], confirm_research=True))
    assert len(calls) == 1
    assert all(item["retryable"] is False for item in results)
    assert [item["error_type"] for item in results] == [code, "RESEARCH_FETCH_STOPPED", "RESEARCH_FETCH_STOPPED"]
    assert result["research_fetch_stop"] == code and result["status"] == "failed"


def test_confirmation_research_plan_review_full_offline_flow(workflow_env):
    env = workflow_env
    assert env.client.get(env.path).json()["goals"][0]["confirmed"] is False
    assert act(env, "bind_goal", goal_artifact_id=env.event["artifact_id"]).status_code == 409
    bound = bind(env)
    assert env.tracker.calls == 0
    assert act(env, "research").status_code == 409
    research = act(env, "research", confirm_research=True)
    assert research.status_code == 200, research.text
    assert research.json()["status"] == "research_ready"
    assert env.tracker.calls == 1
    assert env.tracker.skill == "network-research-and-topic-selection"
    current_task = env.client.get(f"/api/conversations/{env.identifier}/current-task").json()
    assert len(current_task["research"]["candidate_topics"]) == 3
    assert current_task["research"]["research_summary"]
    assert current_task["profile"]["version"] is None  # 旧目标不能追认为来自新档案
    assert "目标文件：" not in env.store.messages(env.identifier)[-2]["content"]
    stale = env.client.post(env.path, json={"action": "research", "expected_revision": bound["revision"], "confirm_research": True})
    assert stale.status_code == 409
    assert act(env, "plan", week_start="2026-09-07").status_code == 409
    assert act(env, "plan", week_start="2026-09-08", accept_limitations=True).status_code == 409
    planned = act(env, "plan", week_start="2026-09-07", accept_limitations=True).json()
    assert planned["status"] == "plan_review"
    assert env.tracker.calls == 1
    artifact = env.store.generation_ui_artifact(env.identifier, planned["plan_artifact_id"])
    assert artifact.scenario == TOPIC_PLAN_SCENARIO
    assert artifact.data["goal_ref"] == env.goal["goal_ref"]
    assert artifact.data["research_limitations"]
    assert "RPA" not in str(artifact.spec["actions"])
    assert [x["id"] for x in artifact.spec["actions"]] == [REVIEW_TOPIC_ACTION]
    for item in artifact.data["contents"]:
        event = {"conversation_id": env.identifier, "message_id": artifact.message_id,
            "artifact_id": artifact.artifact_id, "scenario": TOPIC_PLAN_SCENARIO,
            "event_type": "confirm", "action_id": REVIEW_TOPIC_ACTION,
            "arguments": {"content_id": item["content_id"], "decision": "确认选题"}, "idempotency_key": str(uuid4())}
        response = env.client.post("/api/generation/event", json=event)
        assert response.status_code == 200, response.text
        assert env.client.post("/api/generation/event", json=event).json() == response.json()
    assert env.client.get(env.path).json()["workflow"]["review_status"] == "confirmed_in_conversation"
    # 已完成审核不可使用新的幂等键重开；原事件重放仍返回原回执。
    assert env.client.post("/api/generation/event", json=event).status_code == 200
    assert env.client.post("/api/generation/event", json={**event, "idempotency_key": str(uuid4())}).status_code == 409
    # 从数据库重新加载后仍然保留审核，无模型确认工具也不触发取数。
    reopened = SQLiteConversationStore(env.store.path)
    assert reopened.generation_ui_artifact(env.identifier, artifact.artifact_id).data["approval_status"] == "confirmed_in_conversation"
    event.update(action_id="save_xiaohongshu_draft", idempotency_key=str(uuid4()))
    assert env.client.post("/api/generation/event", json=event).status_code == 400


def test_invalid_draft_survives_restart_and_repairs_without_research(workflow_env):
    env = workflow_env
    env.tracker.mode = "invalid_save"
    failed = start(env).json()
    assert failed["status"] == "failed" and failed["error_code"] == "RESEARCH_SAVE_FAILED"
    assert failed["has_save_draft"] is True and "research_draft" not in failed
    assert failed["research_path"] is None
    assert act(env, "plan", week_start="2026-09-07", accept_limitations=True).status_code == 409
    assert act(env, "resume").json()["status"] == "failed"  # do not guide back to paid collection
    service = ContentWorkflow(env.store, lambda *args: pytest.fail("must not run any model or provider"))
    original = service.get_draft(env.identifier)
    assert isinstance(original["draft"]["candidate_topics"][0]["why_fit_company"], list)
    assert original["validation_errors"][0]["field"] == "candidate_topics.0.why_fit_company"
    # A repeated invalid repair stays recoverable and updates the displayed validation errors.
    invalid = service.act(env.identifier, WorkflowAction(action="save_draft", expected_revision=original["revision"]))
    assert invalid["status"] == "failed" and invalid["research_draft"]
    draft = service.get_draft(env.identifier)
    draft["draft"]["candidate_topics"][0]["why_fit_company"] = "与企业业务相符的合成测试选题。"
    repaired = env.client.post(env.path, json={"action": "save_draft", "expected_revision": draft["revision"],
        "research_draft": draft["draft"]})
    assert repaired.status_code == 200, repaired.text
    saved = repaired.json()
    assert saved["status"] == "research_ready" and saved["has_save_draft"] is False
    assert saved["attempts"] == 1 and env.tracker.calls == 1
    assert env.client.get(f"/api/conversations/{env.identifier}/research-draft").status_code == 409
    assert act(env, "save_draft", research_draft=draft["draft"]).status_code == 409
    assert len(list(env.store.path.parent.glob("research/*/research.json"))) == 1
    plan = act(env, "plan", week_start="2026-09-07", accept_limitations=True)
    assert plan.status_code == 200 and plan.json()["status"] == "plan_review"
    assert env.tracker.calls == 1


def test_draft_route_is_scoped_and_cannot_replace_goal_or_bypass_revision(workflow_env):
    env = workflow_env
    env.tracker.mode = "invalid_save"
    start(env)
    route = f"/api/conversations/{env.identifier}/research-draft"
    draft = env.client.get(route).json()
    assert env.client.get(f"/api/conversations/{env.store.create()}/research-draft").status_code == 409
    assert env.client.post(env.path, json={"action": "save_draft", "expected_revision": 0, "research_draft": draft["draft"]}).status_code == 409
    draft["draft"]["goal_artifact_path"] = "/tmp/untrusted-goal.json"
    assert act(env, "save_draft", research_draft=draft["draft"]).status_code == 409
    assert env.client.get(route).json()["draft"]["goal_artifact_path"] == env.goal["goal_ref"]["artifact_path"]
    env.goals.save(GoalSaveRequest(**goal_input(), previous_goal_artifact_path=env.goal["goal_ref"]["artifact_path"]))
    assert env.client.get(route).status_code == 409
    assert act(env, "save_draft").status_code == 409
    assert env.tracker.calls == 1


def test_corrected_save_during_research_clears_draft_and_keeps_one_artifact(workflow_env):
    env = workflow_env
    env.tracker.mode = "corrected_save"
    result = start(env).json()
    assert result["status"] == "research_ready" and not result["has_save_draft"]
    assert result["validation_errors"] == [] and result["save_error"] is None
    assert env.tracker.calls == 1
    assert len(list(env.store.path.parent.glob("research/*/research.json"))) == 1


def test_legacy_failed_run_has_no_recoverable_draft(workflow_env):
    env = workflow_env
    env.tracker.mode = "no_save"
    result = start(env).json()
    assert result["error_code"] == "RESEARCH_NOT_SAVED" and result["has_save_draft"] is False
    assert act(env, "save_draft").status_code == 409


@pytest.mark.parametrize("mode", ["before_save", "after_save", "no_save", "wrong_goal"])
def test_failure_is_explicit_and_recovery_does_not_fetch(workflow_env, mode):
    env = workflow_env
    env.tracker.mode = mode
    failed = start(env).json()
    assert failed["status"] == "failed"
    assert "synthetic-secret" not in failed["error"]
    resumed = act(env, "resume").json()
    assert resumed["status"] == ("research_ready" if mode == "after_save" else "goal_ready")
    assert env.tracker.calls == 1
    if mode != "after_save":
        assert act(env, "research").status_code == 409
        env.tracker.mode = "success"
        assert act(env, "research", confirm_research=True).json()["status"] == "research_ready"
        assert env.tracker.calls == 2


def test_newer_goal_invalidates_new_research_but_not_saved_research(workflow_env):
    env = workflow_env
    bind(env)
    env.goals.save(GoalSaveRequest(**goal_input(), previous_goal_artifact_path=env.goal["goal_ref"]["artifact_path"]))
    assert act(env, "research", confirm_research=True).status_code == 409
    assert env.tracker.calls == 0


def test_old_goal_card_cannot_confirm_after_new_card_or_workflow(workflow_env):
    env = workflow_env
    new_goal = env.goals.save(GoalSaveRequest(**goal_input()))
    ui = build_operation_goal_ui(new_goal)
    env.store.append_assistant_response(env.identifier, "新的草案", ui_artifacts=[ui])
    assert env.client.post("/api/generation/event", json=env.event).status_code == 409
    assert env.tracker.calls == 0


def test_goal_confirmation_replay_survives_workflow_but_new_request_is_rejected(workflow_env):
    env = workflow_env
    bind(env)
    assert env.client.post("/api/generation/event", json=env.event).status_code == 200
    assert env.client.post("/api/generation/event", json={**env.event, "idempotency_key": str(uuid4())}).status_code == 409
    assert env.tracker.calls == 0


def test_cross_conversation_and_request_payload_cannot_override_binding(workflow_env):
    env = workflow_env
    other = env.store.create()
    request = {"action": "bind_goal", "expected_revision": 0, "goal_artifact_id": env.event["artifact_id"]}
    assert env.client.post(f"/api/conversations/{other}/content-workflow", json=request).status_code == 404
    bind(env)
    assert act(env, "research", confirm_research=True, goal_artifact_path="/tmp/arbitrary").status_code == 422


def test_restart_and_compare_swap_recovery(workflow_env):
    env = workflow_env
    bound = bind(env)
    running = env.store.save_content_workflow(env.identifier, {**bound, "status": "research_running"}, expected_revision=bound["revision"])
    with pytest.raises(GenerationUiActionConflictError):
        env.store.save_content_workflow(env.identifier, bound, expected_revision=bound["revision"])
    service = ContentWorkflow(env.store, lambda *args: pytest.fail("recovery must not run Agent"))
    state = service.get(env.identifier)["workflow"]
    assert state["status"] == "interrupted"
    assert state["revision"] > running["revision"]
    resumed = service.act(env.identifier, WorkflowAction(action="resume", expected_revision=state["revision"]))
    assert resumed["status"] == "goal_ready"


def test_plan_checkpoint_recovery_does_not_duplicate_card(workflow_env):
    env = workflow_env
    start(env)
    planned = act(env, "plan", week_start="2026-09-07", accept_limitations=True).json()
    count = len(env.store.generation_ui_artifacts(env.identifier))
    env.store.save_content_workflow(env.identifier, {**planned, "status": "interrupted", "plan_artifact_id": None}, expected_revision=planned["revision"])
    restored = act(env, "resume").json()
    assert restored["plan_artifact_id"] == planned["plan_artifact_id"]
    assert len(env.store.generation_ui_artifacts(env.identifier)) == count
    assert env.tracker.calls == 1


def test_failed_research_cannot_generate_plan(workflow_env):
    env = workflow_env
    env.tracker.mode = "failed_result"
    assert start(env).json()["status"] == "failed"
    assert act(env, "resume").json()["status"] == "failed"
    assert act(env, "plan", week_start="2026-09-07", accept_limitations=True).status_code == 409


def test_plan_failure_can_resume_without_research(workflow_env, monkeypatch):
    from web_agent import content_workflow
    env = workflow_env
    start(env)
    generator = content_workflow.generate_content_weekly_plan
    monkeypatch.setattr(content_workflow, "generate_content_weekly_plan", lambda *a, **kw: (_ for _ in ()).throw(OSError("synthetic storage failure")))
    assert act(env, "plan", week_start="2026-09-07", accept_limitations=True).json()["status"] == "failed"
    assert act(env, "resume").json()["status"] == "research_ready"
    monkeypatch.setattr(content_workflow, "generate_content_weekly_plan", generator)
    assert act(env, "plan", week_start="2026-09-07", accept_limitations=True).json()["status"] == "plan_review"
    assert env.tracker.calls == 1


def test_review_revision_discard_and_context_guards(workflow_env):
    env = workflow_env
    start(env)
    plan = act(env, "plan", week_start="2026-09-07", accept_limitations=True).json()
    card = env.store.generation_ui_artifact(env.identifier, plan["plan_artifact_id"])
    event = {"conversation_id": env.identifier, "message_id": card.message_id, "artifact_id": card.artifact_id,
        "scenario": TOPIC_PLAN_SCENARIO, "event_type": "confirm", "action_id": REVIEW_TOPIC_ACTION,
        "arguments": {"content_id": card.data["contents"][0]["content_id"], "decision": "要求修改"},
        "idempotency_key": str(uuid4())}
    assert env.client.post("/api/generation/event", json=event).status_code == 409
    event["arguments"]["note"] = "请补充素材来源。"
    assert env.client.post("/api/generation/event", json=event).json()["status"] == "revision_requested"
    wrong_context = {**event, "conversation_id": env.store.create()}
    assert env.client.post("/api/generation/event", json=wrong_context).status_code == 404
    event["arguments"]["image_paths"] = []
    assert env.client.post("/api/generation/event", json=event).status_code == 422
    event["arguments"].pop("image_paths")
    for item in card.data["contents"]:
        event.update(arguments={"content_id": item["content_id"], "decision": "放弃选题"}, idempotency_key=str(uuid4()))
        assert env.client.post("/api/generation/event", json=event).status_code == 200
    assert env.client.get(env.path).json()["workflow"]["review_status"] == "discarded"


def test_manual_research_plan_tool_also_presents_topic_review(workflow_env, monkeypatch):
    from web_agent import app as web_app
    from tools import content_operations
    env = workflow_env
    result = start(env).json()
    monkeypatch.setattr(web_app.LLMClient, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(web_app, "build_memory_client_from_env", lambda: None)
    monkeypatch.setattr(content_operations, "CONTENT_OUTPUT_DIR", env.store.path.parent / "plans")
    monkeypatch.setattr(content_operations, "REPOSITORY_ROOT", env.store.path.parent)
    agent = web_app._default_agent_factory(lambda x: None, web_app.AttachmentRetrievalScope(), mcp_tools=None)
    response = agent.tool_handlers["generate_content_weekly_plan"]("2026-09-07", research_artifact_path=result["research_path"])
    assert response["success"] is True, response
    assert agent.generation_ui_artifacts[0]["scenario"] == TOPIC_PLAN_SCENARIO
    assert REVIEW_TOPIC_ACTION not in agent.tool_handlers


def test_workflow_web_assets_cover_progress_and_mutable_review(workflow_env):
    client = workflow_env.client
    script = client.get("/static/content-workflow.js").text
    assert "setTimeout(refresh, 300)" in script
    assert "void sendChat(`修改周计划：" in script
    assert "/current-task`" in script
    assert 'panel.setAttribute("aria-label", "周计划操作")' in script
    assert "renderResearch" not in script
    assert 'messages.append(panel)' in script
    assert 'onState(payload, busy)' in script
    app_script = client.get("/static/app.js").text
    assert '["content_topic_plan", "content_draft", "content_weekly_plan"].includes(artifact.scenario)' in app_script


def test_research_binds_confirmed_goal_without_separate_ui_action(workflow_env):
    env = workflow_env
    artifact_id = env.event["artifact_id"]
    assert act(env, "research", goal_artifact_id=artifact_id, confirm_research=True).status_code == 409
    assert env.client.post("/api/generation/event", json=env.event).status_code == 200
    assert act(env, "research", goal_artifact_id=artifact_id).status_code == 409
    assert env.tracker.calls == 0
    assert env.client.get(env.path).json()["workflow"] is None
    result = act(env, "research", goal_artifact_id=artifact_id, confirm_research=True)
    assert result.status_code == 200 and result.json()["status"] == "research_ready"
    duplicate = env.client.post(env.path, json={"action": "research", "expected_revision": 0,
        "goal_artifact_id": artifact_id, "confirm_research": True})
    assert duplicate.status_code == 409
    assert env.tracker.calls == 1
