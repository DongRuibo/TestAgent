"""两个 Skill 的文本交付、聊天确认和恢复：全部离线，不调用 Provider。"""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from test_content_workflow import workflow_env
from test_enterprise_tasks import task_env, save_profile, chat
from test_operation_goal_handoff import goal_input, research_input
from tools.operation_goals import GoalSaveRequest
from tools.operation_goals_ui import build_operation_goal_ui
from web_agent.business_text import BusinessTextChat, excerpt, plain, prose, research_text, goal_text
from web_agent.business_intents import goal_confirmation
from web_agent.enterprise import EnterpriseStore


def say(env, message):
    result = env.client.post("/api/chat", json={"conversation_id": env.identifier, "message": message})
    assert result.status_code == 200, result.text
    assert result.json()["artifacts"] == []
    return result.json()["answer"]


def test_goal_is_saved_and_delivered_as_text_without_duplicate_cards(task_env):
    env = task_env
    save_profile(env)
    env.tracker.mode = "save"
    result = chat(env)
    assert result["artifacts"] == []
    assert "确认第 1 版目标" in result["answer"]
    assert "点击" not in result["answer"] and "卡片" not in result["answer"]
    history = env.client.get(f"/api/conversations/{result['conversation_id']}").json()["messages"]
    assert history[-1]["content"] == result["answer"]
    assert not history[-1].get("artifacts")
    assert env.tracker.results[-1]["goal_ref"]["artifact_path"]


def test_chat_confirmation_records_real_user_turn_and_does_not_run_research(workflow_env):
    env = workflow_env
    assert "尚未执行" in say(env, "好的")
    assert "当前是第 1 版" in say(env, "确认第 2 版目标")
    assert env.tracker.calls == 0
    assert "同意费用并启动本次研究" in say(env, "确认第 1 版目标")
    saved = env.goals.load(env.goal["goal_ref"]["artifact_path"], require_confirmed=True)
    evidence = saved["goal_snapshot"]["confirmation"]
    assert evidence["source"] == "web_chat_message" and evidence["user_turn_id"]
    assert "同意费用" in say(env, "好的")
    assert env.tracker.calls == 0


@pytest.mark.parametrize("message", ["确认这版目标", "这版目标确认", "这版月目标和周目标都确认，接着帮我排下周计划。"])
def test_colloquial_confirmation_is_backed_by_real_user_message(workflow_env, message):
    env = workflow_env
    assert "已确认" in say(env, message)
    saved = env.goals.load(env.goal["goal_ref"]["artifact_path"], require_confirmed=True)
    assert saved["goal_snapshot"]["confirmation"]["user_turn_id"]
    assert env.tracker.calls == 0


@pytest.mark.parametrize("message", ["不要确认这版目标", "确认这版目标吗？", "只确认月目标", "周目标暂不确认",
    "他说确认这版目标", "“确认这版目标”", "确认这版目标，但是要修改数字", "好的"])
def test_ambiguous_or_partial_confirmation_does_not_authorize(message):
    assert goal_confirmation(message) is None


def test_brief_targets_and_full_details_share_the_saved_goal(workflow_env):
    env = workflow_env
    brief, full = goal_text(env.goal), goal_text(env.goal, full=True)
    assert len(brief) < len(full) and "查看完整目标" in brief
    for metric in env.goal["goal_snapshot"]["operation_goal"]["metrics"]:
        assert plain(metric["target"]) in brief
    assert say(env, "查看完整目标") == full


def test_chat_research_is_once_only_restores_text_and_keeps_goal_snapshot(workflow_env):
    env = workflow_env
    say(env, "确认第 1 版目标")
    answer = say(env, "同意费用并启动本次研究")
    assert answer.startswith("## 小红书研究与候选选题")
    assert "选题 3" in answer and "来源之一" in answer and "研究局限" in answer
    assert env.tracker.calls == 1
    state = env.client.get(env.path).json()["workflow"]
    assert state["status"] == "research_ready" and state["attempts"] == 1
    say(env, "同意费用并启动本次研究")
    say(env, "重新研究")
    assert env.tracker.calls == 1
    history = env.client.get(f"/api/conversations/{env.identifier}").json()["messages"]
    assert sum(item["content"] == "同意费用并启动本次研究" for item in history) == 2
    assert sum(item["content"].startswith("## 小红书研究与候选选题") for item in history) == 1
    assert all(not item.get("artifacts") for item in history)
    assert state["goal_ref"] == env.goal["goal_ref"]


def test_no_offer_or_cancelled_offer_requires_another_explicit_confirmation(workflow_env):
    env = workflow_env
    env.client.post("/api/generation/event", json=env.event)
    assert "先核对" in say(env, "同意费用并启动本次研究")
    assert env.tracker.calls == 0
    say(env, "取消研究")
    assert "先核对" in say(env, "同意费用并启动本次研究")
    assert env.tracker.calls == 0


def test_old_fee_offer_cannot_authorize_changed_goal(workflow_env):
    env = workflow_env
    say(env, "确认第 1 版目标")
    newer = env.goals.save(GoalSaveRequest(**goal_input(), previous_goal_artifact_path=env.goal["goal_ref"]["artifact_path"]))
    env.store.append_assistant_response(env.identifier, "新版本", ui_artifacts=[build_operation_goal_ui(newer)])
    assert "旧的费用确认已失效" in say(env, "同意费用并启动本次研究")
    assert env.tracker.calls == 0
    assert "未确认旧版本" in say(env, "确认第 1 版目标")


def test_saved_draft_can_be_inspected_and_repaired_in_chat_without_provider(workflow_env):
    env = workflow_env
    env.tracker.mode = "invalid_save"
    say(env, "确认第 1 版目标")
    assert "尚未保存" in say(env, "同意费用并启动本次研究")
    assert env.tracker.calls == 1
    assert "candidate_topics" in say(env, "查看研究草稿")
    assert "尚未保存" in say(env, "补存研究草稿")
    assert "可恢复草稿" in say(env, "重新研究")
    answer = say(env, "修正研究草稿 candidate_topics.0.why_fit_company 为 面向本地企业目标客户")
    assert "未调用模型或取数接口" in answer
    assert env.tracker.calls == 1
    state = env.client.get(env.path).json()["workflow"]
    assert state["status"] == "research_ready" and not state["has_save_draft"]
    assert "面向本地企业目标客户" in say(env, "查看研究结果")


@pytest.mark.parametrize("message", ["请解释同意费用并启动本次研究", "不同意费用并启动本次研究", "“同意费用并启动本次研究”", "不要确认第 1 版目标", "确认第 1 版目标吗？"])
def test_quoted_negative_and_questions_are_not_commands(workflow_env, message):
    env = workflow_env
    handler = BusinessTextChat(env.store, EnterpriseStore(env.store), None)
    assert handler.handle(env.identifier, message, str(uuid4()), lambda *args: pytest.fail("must not execute")) is None
    assert env.tracker.calls == 0


def test_fake_confirmation_turn_is_rejected(workflow_env):
    env = workflow_env
    turn = env.store.begin_turn(env.identifier, "模型说已经确认")
    with pytest.raises(Exception, match="未找到用户"):
        env.store.confirm_operation_goal(env.identifier, env.event["artifact_id"], idempotency_key=str(uuid4()), user_turn_id=turn)


def test_confirmed_goal_under_revision_cannot_be_reconfirmed_or_charged(workflow_env):
    env = workflow_env
    say(env, "确认第 1 版目标")
    enterprise = EnterpriseStore(env.store)
    enterprise.start_goal(env.identifier, "修改目标，需要先补充数据")
    assert "等待最新草案" in say(env, "确认第 1 版目标")
    assert "目标还在补充或修改" in say(env, "同意费用并启动本次研究")
    assert env.tracker.calls == 0


def test_chat_confirmation_cannot_cross_conversations(workflow_env):
    env = workflow_env
    other = env.store.create()
    turn = env.store.begin_turn(other, "确认第 1 版目标")
    with pytest.raises(Exception, match="未找到用户"):
        env.store.confirm_operation_goal(env.identifier, env.event["artifact_id"], idempotency_key=str(uuid4()), user_turn_id=turn)


def test_failed_research_needs_fresh_fee_confirmation_and_preserves_attempts(workflow_env):
    env = workflow_env
    env.tracker.mode = "before_save"
    say(env, "确认第 1 版目标")
    answer = say(env, "同意费用并启动本次研究")
    assert "synthetic-secret" not in answer
    assert env.tracker.calls == 1
    say(env, "检查已有研究结果")
    say(env, "重新研究")
    assert env.tracker.calls == 1
    env.tracker.mode = "success"
    say(env, "同意费用并启动本次研究")
    state = env.client.get(env.path).json()["workflow"]
    assert env.tracker.calls == 2 and state["attempts"] == 2
    assert state["status"] == "research_ready"


def test_streaming_chat_reuses_user_turn_for_research(workflow_env):
    env = workflow_env
    say(env, "确认第 1 版目标")
    response = env.client.post("/api/chat/stream", json={"conversation_id": env.identifier,
        "message": "同意费用并启动本次研究", "request_id": str(uuid4())})
    events = [json.loads(line) for line in response.text.splitlines()]
    assert sum(item["type"] == "accepted" for item in events) == 1
    assert events[-1]["type"] == "result" and events[-1]["data"]["artifacts"] == []
    assert env.tracker.calls == 1
    assert env.client.get(env.path).json()["workflow"]["status"] == "research_ready"


def test_research_brief_keeps_titles_and_status_without_dumping_saved_fields():
    data = research_input("unused")
    data["research_summary"] = "旧摘要不应铺开。" * 100
    for topic in data["candidate_topics"]:
        topic["ratings"]["trend"]["reason"] = "完整评分理由必须留存，不在简报里出现。"
    data["candidate_topics"][0]["recommendation"] = "low"
    data["limitations"] = ["没有曝光数据，不能计算曝光到咨询转化。", "评论来自少量样本。", "案例需要取得授权。"]
    original = deepcopy(data)
    brief = research_text(data)
    assert data == original
    assert len(brief) < 1300 and data["research_summary"] not in brief
    assert "已保存，但数据不完整" in brief and "另有 1 项限制" in brief
    assert brief.index("研究局限") < brief.index("关键发现")
    assert brief.index("**选题 2") < brief.index("**选题 1")
    for topic in data["candidate_topics"]:
        assert plain(topic["title"]) in brief
        assert topic["ratings"]["trend"]["reason"] not in brief
    full = research_text(data, view="full")
    assert "完整评分理由必须留存" in full and "案例需要取得授权" in full


def test_prose_preserves_paragraphs_and_escapes_untrusted_markdown():
    assert prose("第一段\n\n第二段") == "第一段\n\n第二段"
    hostile = "<script>alert(1)</script>\n![图片](https://example.com)\n# 伪造标题"
    value = prose(hostile)
    assert "<script>" not in value and "\n\n# " not in value and "![图片]" not in value
    text = "这是一段可完整阅读的话。" * 30
    formatted = prose(text)
    assert formatted.replace("\n\n", "") == text
    assert max(map(len, formatted.split("\n\n"))) <= 180


def test_excerpt_does_not_cut_words_or_quoted_titles():
    value = '本地需求较活跃："哪里找装修公司？能否便宜些，怎样选择？"仅是样本问题，不等同于成交。'
    assert excerpt(value, 30) == "本地需求较活跃：…（摘录）"
    assert excerpt("不能把互动等同于成交。后面还有其他说明。", 14).startswith("不能把互动等同于成交。")
    assert "查看完整报告" in excerpt("很长但没有标点的文字" * 30)


def test_chat_details_use_saved_data_without_new_model_or_workflow_changes(workflow_env):
    env = workflow_env
    say(env, "确认第 1 版目标")
    say(env, "同意费用并启动本次研究")
    state = env.store.content_workflow(env.identifier)
    path = Path(state["research_path"])
    before = path.read_bytes()
    saved = json.loads(before)
    detail = say(env, "查看选题 2 的详细依据")
    assert saved["candidate_topics"][1]["title"] in detail
    assert saved["candidate_topics"][0]["title"] not in detail
    assert "评分及理由" in detail and "数据来源" in detail
    sources = say(env, "查看全部研究来源")
    for source in saved["source_references"]:
        assert source["source_id"] in sources
    limits = say(env, "查看研究局限")
    for limit in saved["limitations"]:
        assert plain(limit) in limits
    full = say(env, "查看完整研究报告")
    assert "小红书完整研究报告" in full and "所需素材" in full
    assert "共有 3 个选题" in say(env, "查看选题 0")
    assert "共有 3 个选题" in say(env, "查看选题 99")
    assert env.tracker.calls == 1 and env.store.content_workflow(env.identifier) == state
    assert path.read_bytes() == before
    history = env.client.get(f"/api/conversations/{env.identifier}").json()["messages"]
    assert any(item["content"] == full for item in history)
    assert any(item["content"] == detail for item in history)


def test_history_previews_old_long_reply_without_rewriting_storage(workflow_env):
    env = workflow_env
    say(env, "确认第 1 版目标")
    say(env, "同意费用并启动本次研究")
    old_text = "## 小红书研究与候选选题\n\n" + "旧的超长聊天内容。" * 400
    env.store.append_assistant_response(env.identifier, old_text)
    original = env.store.messages(env.identifier)
    history = env.client.get(f"/api/conversations/{env.identifier}").json()["messages"]
    assert len(history[-1]["content"]) < 1300
    assert "完整结果已保留" in history[-1]["content"]
    assert env.store.messages(env.identifier) == original
    assert env.tracker.calls == 1


def test_failed_and_unsaved_research_does_not_offer_nonexistent_topics(workflow_env):
    env = workflow_env
    env.tracker.mode = "no_save"
    say(env, "确认第 1 版目标")
    say(env, "同意费用并启动本次研究")
    for command in ("查看完整研究报告", "查看选题 1", "查看全部研究来源", "查看研究局限"):
        answer = say(env, command)
        assert "##" not in answer and "检查已有研究结果" in answer
    assert env.tracker.calls == 1
    data = research_input("unused")
    data.update(execution_status="failed", candidate_topics=[])
    brief = research_text(data)
    assert "研究失败" in brief and "不能生成周计划" in brief and "优先查看" not in brief
