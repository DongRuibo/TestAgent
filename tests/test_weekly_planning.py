"""新周计划的真实本地API/保存链；模型与研究均使用明确的离线替身。"""

from datetime import datetime
from pathlib import Path
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
from web_agent.app import create_app
from web_agent.conversations import SQLiteConversationStore
from web_agent import weekly_planning


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / "deliverables/enterprise-weekly-operation-plan-v1-1/validation/example-plan.md").read_text()
NOW = datetime.fromisoformat("2026-09-17T09:00:00+08:00")


def plan_markdown(data):
    inputs, goal, research = data["inputs"], data["goal"], data["research"]
    text = EXAMPLE.replace("3f7bd69e-bf6c-47e5-a98b-d2e893e0d409", inputs["plan_id"])
    text = text.replace("示例家居企业", research["company_name"])
    text = text.replace("SAMPLE-G01", goal["goal_ref"]["goal_id"])
    text = text.replace("SAMPLE-R01:S01", data["source_index"][0]["source_id"])
    text = text.replace("SAMPLE-R01", research["research_id"])
    text = text.replace("离线虚构来源S01，不存在实际平台笔记", data["source_index"][0]["location"])
    start, end = text.index("## 4."), text.index("## 5.")
    lines = text[start:end].splitlines()
    lines = [line + (" 可用情况与依据 |" if "图片需求" in line else "---|" if "|---" in line
        else " 待核实：缺件时延后 |") if line.startswith("|") else line for line in lines]
    text = text[:start] + "\n".join(lines) + "\n\n" + text[end:]
    return text


@pytest.fixture
def weekly_env(tmp_path, monkeypatch):
    monkeypatch.setattr(operation_goals, "OPERATION_GOAL_DIR", tmp_path / "goals")
    monkeypatch.setattr(content_research, "CONTENT_RESEARCH_OUTPUT_DIR", tmp_path / "research")
    monkeypatch.setattr(weekly_planning, "WEEKLY_DIR", tmp_path / "plans")
    monkeypatch.setattr(weekly_planning.WeeklyPlanning, "now", lambda self: NOW)
    goals = OperationGoalStore()
    payload = goal_input()
    payload["operation_goal"]["period"] = {"start": "2026-09-14", "end": "2026-09-20"}
    goal = goals.save(GoalSaveRequest(**payload))
    store = SQLiteConversationStore(tmp_path / "chat.sqlite3")
    identifier = store.create()
    ui = build_operation_goal_ui(goal)
    store.append_assistant_response(identifier, "离线目标", ui_artifacts=[ui])
    research = ContentResearchTools(output_dir=tmp_path / "research")
    tracker = SimpleNamespace(research_calls=0, prepare_calls=0, compose_calls=0,
        mode="success", contexts=[], last_markdown=None, last_save=None, force_research=False)

    class Agent:
        def __init__(self):
            self.tool_handlers = research.handlers()
            self.tool_handlers["social_get_post_detail"] = synthetic_detail
            self.generation_ui_artifacts = []

        def run(self, task, *, history, episode_id, skill_name=None, auto_select_skill=True):
            assert history == [] and auto_select_skill is False
            if skill_name == "network-research-and-topic-selection":
                tracker.research_calls += 1
                tracker.contexts.append(task)
                if tracker.mode == "research_error":
                    raise RuntimeError("synthetic-private-provider-error")
                payload = research_input(goal["goal_ref"]["artifact_path"])
                for source in payload["source_references"]:
                    self.tool_handlers["social_get_post_detail"](url=source["url"])
                if tracker.mode == "research_invalid":
                    payload["candidate_topics"][0]["why_fit_company"] = ["invalid"]
                self.tool_handlers["save_content_research_result"](**payload)
                return "研究回执"
            assert skill_name == "enterprise-weekly-operation-plan"
            data = self.tool_handlers["load_weekly_plan_inputs"]()
            if data["stage"] == "prepare":
                tracker.prepare_calls += 1
                self.tool_handlers["choose_weekly_research"](needs_research=tracker.force_research or not bool(data["research"]),
                    reason="需要与本周目标相关的图文候选" if not data["research"] else "已有研究与本周方向和平台一致",
                    research_question="围绕本周小户型收纳目标寻找有依据的图文候选。")
                return "准备完成"
            tracker.compose_calls += 1
            if tracker.mode == "no_plan_save":
                return "计划已经生成（实际没有保存）"
            text = data["previous_markdown"] or plan_markdown(data)
            if data["revision_request"]:
                text = text.replace("2026-09-19 | 18:00", "2026-09-19 | 19:00")
            if tracker.mode == "bad_company":
                text = text.replace(data["research"]["company_name"], "另一家企业")
            if tracker.mode == "bad_source":
                text = text.replace(data["source_index"][0]["source_id"], "unbound:source")
            if tracker.mode == "invalid_time":
                text = text.replace("2026-09-18 10:00", "2026-09-16 10:00")
            tracker.last_markdown = text
            tracker.last_save = self.tool_handlers["save_weekly_plan"](markdown=text)
            if tracker.mode == "after_save_error":
                raise RuntimeError("synthetic-private-provider-error")
            return "模型返回"

    client = TestClient(create_app(lambda *args: Agent(), conversation_store=store))
    env = SimpleNamespace(client=client, store=store, identifier=identifier, tracker=tracker, goals=goals, goal=goal,
        research=research, path=f"/api/conversations/{identifier}/content-workflow")
    assert "此版本目标已确认" in say(env, "确认第 1 版目标")
    return env


def say(env, message):
    response = env.client.post("/api/chat", json={"conversation_id": env.identifier, "message": message})
    assert response.status_code == 200, response.text
    assert response.json()["artifacts"] == []
    return response.json()["answer"]


def state(env):
    return env.store.content_workflow(env.identifier)


def prepare(env):
    return say(env, "制定周计划：2026-09-14；测试员工甲设计师提供案例照片，测试员工乙项目经理提供近照，测试员工丙客服记录咨询")


def finish(env):
    prepare(env)
    say(env, "启动研究")
    return say(env, "同意费用并启动本次研究")


def test_prepare_authorize_research_compose_and_download(weekly_env):
    env = weekly_env
    assert "补充研究" in prepare(env)
    assert state(env)["status"] == "weekly_waiting_research"
    assert env.tracker.research_calls == 0 and env.tracker.compose_calls == 0
    say(env, "启动研究")
    answer = say(env, "同意费用并启动本次研究")
    assert "下载周运营计划" in answer, answer
    assert state(env)["status"] == "plan_ready"
    assert (env.tracker.prepare_calls, env.tracker.research_calls, env.tracker.compose_calls) == (1, 1, 1)
    assert "测试员工" not in env.tracker.contexts[0]
    assert "小户型收纳目标" in env.tracker.contexts[0]
    assert Path(state(env)["weekly_saved"]["path"]).read_text() == env.tracker.last_markdown
    response = env.client.get(f"/api/conversations/{env.identifier}/weekly-plan")
    assert response.status_code == 200 and response.text == env.tracker.last_markdown
    assert "attachment" in response.headers["content-disposition"]
    assert "weekly_draft" not in env.client.get(env.path).json()["workflow"]
    assert "markdown" not in env.client.get(env.path).json()["workflow"]["weekly_saved"]


def test_existing_research_is_reused_and_revisions_keep_ids_and_history(weekly_env):
    env = weekly_env
    say(env, "同意费用并启动本次研究")
    assert env.tracker.research_calls == 1
    assert "下载周运营计划" in prepare(env)
    original = state(env)["weekly_saved"]
    assert "下载周运营计划" in say(env, "修改周计划：发布时间改到周六19点")
    revised = state(env)["weekly_saved"]
    assert revised["version"] == 2 and revised["content_ids"] == original["content_ids"]
    assert env.tracker.research_calls == 1 and env.tracker.prepare_calls == 1
    assert "19:00" in revised["markdown"]
    route = f"/api/conversations/{env.identifier}/weekly-plan"
    assert env.client.get(route + "?version=1").text == original["markdown"]
    assert env.client.get(route + "?version=2").text == revised["markdown"]
    assert env.client.get(route + "?version=999").status_code == 404
    other = env.store.create()
    assert env.client.get(f"/api/conversations/{other}/weekly-plan?version=1").status_code == 404


@pytest.mark.parametrize("mode", ["no_plan_save", "invalid_time", "bad_company", "bad_source"])
def test_invalid_plan_cannot_claim_success_and_resume_keeps_research(weekly_env, mode):
    env = weekly_env
    env.tracker.mode = mode
    answer = finish(env)
    assert state(env)["status"] == "failed" and "下载周运营计划" not in answer
    assert env.client.get(f"/api/conversations/{env.identifier}/weekly-plan").status_code == 404
    env.tracker.mode = "success"
    assert "下载周运营计划" in say(env, "继续周计划")
    assert env.tracker.research_calls == 1


def test_save_then_model_error_does_not_lose_success(weekly_env):
    env = weekly_env
    env.tracker.mode = "after_save_error"
    assert "下载周运营计划" in finish(env)
    calls = env.tracker.compose_calls
    assert "下载周运营计划" in say(env, "继续周计划")
    assert env.tracker.compose_calls == calls and env.tracker.research_calls == 1


def test_research_error_preserves_context_and_needs_fresh_authorization(weekly_env):
    env = weekly_env
    env.tracker.mode = "research_error"
    answer = finish(env)
    assert "synthetic-private" not in answer and state(env)["status"] == "failed"
    env.tracker.mode = "success"
    say(env, "继续周计划")
    assert env.tracker.research_calls == 1 and state(env)["status"] == "weekly_waiting_research"
    say(env, "启动研究")
    assert "下载周运营计划" in say(env, "同意费用并启动本次研究")
    assert env.tracker.research_calls == 2


def test_research_draft_repair_continues_plan_without_recollection(weekly_env):
    env = weekly_env
    env.tracker.mode = "research_invalid"
    finish(env)
    assert state(env).get("research_draft")
    env.tracker.mode = "success"
    answer = say(env, "修正研究草稿 candidate_topics.0.why_fit_company 为 适用于本企业的小户型目标客户")
    assert "下载周运营计划" in answer
    assert env.tracker.research_calls == 1


def test_scope_change_invalidates_old_fee_offer(weekly_env):
    env = weekly_env
    prepare(env)
    # 确认目标时的旧授权提示不能启动新周计划研究。
    assert "旧的费用确认已失效" in say(env, "同意费用并启动本次研究")
    assert env.tracker.research_calls == 0


def test_expired_or_missing_inputs_are_rejected_before_model(weekly_env):
    env = weekly_env
    assert "谁能提供" in say(env, "制定周计划")
    assert "没有覆盖" in say(env, "制定周计划：2026-09-07；暂无人员名单")
    assert env.tracker.prepare_calls == 0


def test_business_reply_omits_internal_reason_and_failed_research_resume(weekly_env):
    env = weekly_env
    prepare(env)
    current = state(env)
    current["weekly_choice"]["reason"] = "load_weekly_plan_inputs: research=null " * 20
    reply = weekly_planning.weekly_reply(env.identifier, current)
    assert "load_weekly" not in reply and len(reply) < 200
    current.update(status="failed", error_code="RESEARCH_FAILED")
    env.store.save_content_workflow(env.identifier, current, expected_revision=current["revision"])
    counts = (env.tracker.prepare_calls, env.tracker.research_calls, env.tracker.compose_calls)
    reply = say(env, "继续周计划")
    assert "查看研究局限" in reply and "回复“继续周计划”" not in reply
    assert (env.tracker.prepare_calls, env.tracker.research_calls, env.tracker.compose_calls) == counts


def test_current_goal_version_checked_before_revision(weekly_env):
    env = weekly_env
    finish(env)
    payload = goal_input()
    payload["operation_goal"]["period"] = {"start": "2026-09-14", "end": "2026-09-20"}
    env.goals.save(GoalSaveRequest(**payload, previous_goal_artifact_path=env.goal["goal_ref"]["artifact_path"]))
    calls = env.tracker.compose_calls
    assert "未执行" in say(env, "修改周计划：发布时间改到周六19点")
    assert env.tracker.compose_calls == calls


def test_api_generation_and_stale_revision(weekly_env):
    env = weekly_env
    request = {"action": "weekly_plan", "expected_revision": 0, "goal_artifact_id":
        env.store.generation_ui_artifacts(env.identifier)[-1].artifact_id,
        "week_start": "2026-09-14", "plan_notes": "暂无人员名单，先保留提交人缺口"}
    response = env.client.post(env.path, json=request)
    assert response.status_code == 200 and response.json()["status"] == "weekly_waiting_research"
    assert env.client.post(env.path, json=request).status_code == 409
    assert env.tracker.prepare_calls == 1


@pytest.mark.parametrize("message", ["就用小周那几张照片，别再加材料了，改一下计划。",
    "就三张现有照片，不补拍，也没有尺寸图。按这三张来改，别再列六种照片了。"])
def test_colloquial_revision_routes_to_existing_plan_without_research(weekly_env, message):
    from web_agent.skill_routing import route_skill
    env = weekly_env
    finish(env)
    env.client = TestClient(create_app(lambda *args: None, conversation_store=env.store,
        skill_router=route_skill))
    # The model failure is expected; reaching a revision still records the user's exact request.
    say(env, message)
    assert state(env)["weekly_revision"] == message.rstrip("。")
    assert state(env)["weekly_saved"]["version"] == 1
    assert env.tracker.research_calls == 1


def test_material_quote_allows_explanation_but_rejects_fabricated_evidence(weekly_env):
    from web_agent.content_workflow import ContentWorkflow
    env = weekly_env
    finish(env)
    current = state(env)
    # Use the production validator with a fully valid saved plan and bound original text.
    service = ContentWorkflow(env.store, lambda *args: None)
    research = service._research(current)
    original = current["weekly_saved"]["markdown"]
    quote = "测试员工甲设计师提供案例照片"
    good = original.replace("待核实：缺件时延后", f"已确认：“{quote}”；实际收到后检查，缺件时延后")
    service.weekly.persist(env.identifier, current, good, research)
    bad = good.replace(quote, "测试员工丁已经提供全部尺寸图")
    with pytest.raises(ValueError, match="逐字引用"):
        service.weekly.persist(env.identifier, state(env), bad, research)


def test_later_revision_preserves_prior_user_material_evidence(weekly_env):
    from web_agent.content_workflow import ContentWorkflow
    env = weekly_env
    finish(env)
    material = "就用三张现有照片，不补拍"
    say(env, "修改周计划：" + material)
    say(env, "修改周计划：发布时间晚一小时，其他不变")
    current = state(env)
    assert current["weekly_revisions"] == [material, "发布时间晚一小时，其他不变"]
    # Old conversations did not persist revision history; recover actual user evidence after this goal.
    current.pop("weekly_revisions")
    service = ContentWorkflow(env.store, lambda *args: None)
    assert any(material in item for item in service.weekly.user_requirements(env.identifier, current))
    research = service._research(current)
    original = current["weekly_saved"]["markdown"]
    good = original.replace("待核实：缺件时延后", f"已确认：“{material}”；具体画面收到后核查")
    service.weekly.persist(env.identifier, current, good, research)
    env.store.append_assistant_response(env.identifier, "模型自行声称五张照片已经收到")
    bad = good.replace(material, "五张照片已经收到")
    with pytest.raises(ValueError, match="逐字引用"):
        service.weekly.persist(env.identifier, state(env), bad, research)


def test_local_write_failure_restores_draft_without_model_or_research(weekly_env, monkeypatch):
    env = weekly_env
    original = weekly_planning.os.link
    monkeypatch.setattr(weekly_planning.os, "link", lambda *args: (_ for _ in ()).throw(OSError("offline disk error")))
    finish(env)
    assert state(env)["status"] == "failed" and state(env)["weekly_draft"]
    counts = (env.tracker.compose_calls, env.tracker.research_calls)
    monkeypatch.setattr(weekly_planning.os, "link", original)
    assert "下载周运营计划" in say(env, "继续周计划")
    assert (env.tracker.compose_calls, env.tracker.research_calls) == counts


def test_real_agent_loop_tool_schemas_permissions_and_large_input(weekly_env):
    from agent.loop import AgentLoop, DEFAULT_TOOL_HANDLERS
    from agent.skill_tools import register_skill_tools
    from agent.skills import SkillCatalog
    from test_skill_runtime import FakeMessage, _call
    import json

    env = weekly_env
    catalog = SkillCatalog.from_directory(ROOT / ".agents/skills")
    sizes = []

    class ScriptedClient:
        def __init__(self):
            self.step = 0

        def complete(self, messages, tools=None):
            self.step += 1
            exposed = {tool["function"]["name"] for tool in tools}
            if "load_weekly_plan_inputs" not in exposed:
                if self.step == 1:
                    return FakeMessage(None, [_call("social_get_post_detail", json.dumps({"url": research_input(env.goal["goal_ref"]["artifact_path"])["source_references"][0]["url"]}))])
                if self.step == 2:
                    return FakeMessage(None, [_call("save_content_research_result", json.dumps(research_input(env.goal["goal_ref"]["artifact_path"])))])
                return FakeMessage("研究已保存")
            assert not exposed & {"run_code", "social_search_posts", "save_content_research_result"}
            if self.step == 1:
                return FakeMessage(None, [_call("load_weekly_plan_inputs")])
            if self.step == 2:
                result = next(message["content"] for message in reversed(messages) if message["role"] == "tool")
                data = json.loads(result)
                assert not data.get("truncated"), data.keys()
                assert data["template"].endswith("|\n") and data["inputs"]["staff_and_requirements"]
                sizes.append(len(result))
                name = "choose_weekly_research" if data["stage"] == "prepare" else "save_weekly_plan"
                payload = ({"needs_research": not bool(data["research"]), "reason": "需要适用候选", "research_question": "研究本周小户型方向"}
                    if data["stage"] == "prepare" else {"markdown": plan_markdown(data)})
                return FakeMessage(None, [_call(name, json.dumps(payload))])
            result = json.loads(next(message["content"] for message in reversed(messages) if message["role"] == "tool"))
            assert result["success"], result
            return FakeMessage("阶段完成")

    def factory(trace, scope):
        handlers = register_skill_tools(DEFAULT_TOOL_HANDLERS, catalog)
        handlers.update(env.research.handlers())
        handlers["social_get_post_detail"] = synthetic_detail
        return AgentLoop(ScriptedClient(), trace=trace, skill_catalog=catalog,
            tool_handlers=handlers)

    env.client = TestClient(create_app(factory, conversation_store=env.store))
    answer = say(env, "制定周计划：2026-09-14；暂无人员名单；" + "业务背景" * 2200)
    assert "补充研究" in answer
    say(env, "启动研究")
    assert "下载周运营计划" in say(env, "同意费用并启动本次研究")
    assert max(sizes) > 8000


def test_stop_reaches_weekly_agent_and_preserves_recoverable_state(weekly_env):
    import json
    import threading
    from llm.client import LLMStreamCancelled

    env = weekly_env
    started, release = threading.Event(), threading.Event()

    class BlockingAgent:
        def __init__(self):
            self.tool_handlers = {}

        def run(self, task, *, history, episode_id, skill_name, auto_select_skill, is_cancelled):
            assert skill_name == "enterprise-weekly-operation-plan" and callable(is_cancelled)
            self.tool_handlers["load_weekly_plan_inputs"]()
            started.set()
            assert release.wait(timeout=3)
            assert is_cancelled()
            raise LLMStreamCancelled("cancelled")

    env.client = TestClient(create_app(lambda *args: BlockingAgent(), conversation_store=env.store))
    request_id, result = str(uuid4()), {}

    def send():
        result["response"] = env.client.post("/api/chat/stream", json={"request_id": request_id,
            "conversation_id": env.identifier, "message": "制定周计划：暂无人员名单"})

    thread = threading.Thread(target=send)
    thread.start()
    assert started.wait(timeout=3)
    try:
        assert env.client.delete(f"/api/chat/stream/{request_id}").status_code == 202
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    events = [json.loads(line) for line in result["response"].text.splitlines()]
    assert events[-1]["type"] == "cancelled"
    assert state(env)["status"] == "failed" and state(env)["weekly_inputs"]
    assert not state(env).get("weekly_saved") and env.tracker.research_calls == 0


@pytest.mark.parametrize('week_start,expected_error', [(None, False), ('2026-09-21', False), ('2026-09-28', True)])
def test_combined_goal_uses_confirmed_week_not_current_week(weekly_env, week_start, expected_error):
    from test_operation_goal_handoff import combined_goal_input
    env = weekly_env
    payload = combined_goal_input()
    payload['weekly_goal']['period'] = {'start': '2026-09-21', 'end': '2026-09-27'}
    goal = env.goals.save(GoalSaveRequest(**payload))
    env.store.append_assistant_response(env.identifier, '组合草案', ui_artifacts=[build_operation_goal_ui(goal)])
    assert '月目标和简单周目标已确认' in say(env, '确认第 1 版目标')
    result = say(env, '制定周计划：' + (week_start or '') + '；暂无人员名单；素材截止2026-09-22')
    if expected_error:
        assert '所选周与已确认的简单周目标不一致' in result
        assert env.tracker.prepare_calls == 0
    else:
        assert '补充研究' in result
        assert state(env)['weekly_inputs']['start'] == '2026-09-21'
        assert state(env)['weekly_inputs']['end'] == '2026-09-27'
        assert state(env)['goal_ref'] == goal['goal_ref']
