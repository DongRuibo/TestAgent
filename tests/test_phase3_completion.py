"""完成声明必须经过宿主目标核验；普通聊天保留原返回契约。"""

import json

import pytest

from agent.completion import CompletionContract, CompletionDecision
from agent.context import MemoryRecallPolicy
from evals.agent_runner import RecordedSDK, RecordedAgent, RecordedLLM, Reply, ToolCall
from llm.client import LLMConfig
from test_content_production import production_env


@pytest.mark.parametrize("claim", ['{"completed": true}', '{"completed": "yes"}'])
def test_unbound_completion_claim_is_not_authoritative(claim):
    sdk = RecordedSDK([])
    sdk.script = iter([Reply(content=claim)])
    agent = RecordedAgent(RecordedLLM(LLMConfig("synthetic", "completion"), sdk_client=sdk),
        trace=None, tool_definitions={}, tool_handlers={}, memory_recall_policy=MemoryRecallPolicy("off"))
    assert agent.run("执行一个业务任务", auto_select_skill=False) == '{"completed": false}'
    assert agent.last_model_answer == claim
    assert agent.last_completion_receipt["status"] == "needs_review"
    assert agent.last_completion_receipt["reason"] == "HOST_CONTRACT_REQUIRED"


@pytest.mark.parametrize("scenario", ["no_tools", "failed_tool", "incomplete_ack", "readback_failed",
    "waiting_approval", "model_false_after_success", "verified_success", "natural_language", "verifier_exception"])
def test_bound_completion_uses_persisted_evidence(tmp_path, scenario):
    path = tmp_path / "result.txt"
    events = []
    sdk = RecordedSDK(events)
    called = []
    def save():
        called.append(True)
        if scenario == "failed_tool":
            return {"success": False}
        if scenario != "incomplete_ack":
            path.write_text("宿主绑定目标", encoding="utf-8")
        return {"success": True}
    def verify():
        if scenario == "verifier_exception":
            raise OSError("合成读回失败")
        ok = path.is_file() and path.read_text("utf-8") == "宿主绑定目标" and scenario not in {"readback_failed", "waiting_approval"}
        return CompletionDecision(status="completed" if ok else "needs_review",
            reason="VERIFIED" if ok else "BUSINESS_EVIDENCE_MISSING",
            evidence={"artifact": "synthetic-result"} if ok else {})
    script = []
    if scenario not in {"no_tools", "natural_language"}:
        script.append(Reply(tool_calls=[ToolCall(id="save", name="save_result", arguments="{}")]))
    claim = "任务已全部完成" if scenario == "natural_language" else json.dumps({"completed": scenario != "model_false_after_success"})
    sdk.script = iter([*script, Reply(content=claim)])
    definition = {"type": "function", "function": {"name": "save_result", "description": "合成保存",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    agent = RecordedAgent(RecordedLLM(LLMConfig("synthetic", "completion"), sdk_client=sdk),
        trace=None, tool_definitions={"save_result": definition}, tool_handlers={"save_result": save},
        tool_budgets={"save_result": 1}, memory_recall_policy=MemoryRecallPolicy("off"),
        completion_contract=CompletionContract("save_candidate", verify))
    answer = json.loads(agent.run("保存宿主指定的目标", auto_select_skill=False))
    assert answer["completed"] is (scenario in {"model_false_after_success", "verified_success"})
    assert answer["status"] == ("completed" if answer["completed"] else "needs_review")
    assert agent.last_model_answer == claim
    assert len(called) == (0 if scenario in {"no_tools", "natural_language"} else 1)
    assert agent.last_completion_receipt["task_type"] == "save_candidate"


def test_completed_decision_requires_evidence_and_plain_chat_is_unchanged():
    with pytest.raises(ValueError):
        CompletionDecision(status="completed", reason="没有证据")
    sdk = RecordedSDK([])
    sdk.script = iter([Reply(content="你好")])
    agent = RecordedAgent(RecordedLLM(LLMConfig("synthetic", "chat"), sdk_client=sdk),
        trace=None, tool_definitions={}, tool_handlers={}, memory_recall_policy=MemoryRecallPolicy("off"))
    assert agent.run("你好", auto_select_skill=False) == "你好"
    assert agent.last_completion_receipt is None


def test_bound_stream_only_emits_host_answer_and_next_run_clears_receipt():
    from agent.loop import AgentLoop
    from test_agent_loop import FakeStreamingLLMClient, FakeMessage
    client = FakeStreamingLLMClient([FakeMessage("所有操作已经完成"), FakeMessage("普通回答")])
    agent = AgentLoop(client, trace=None, tool_definitions={}, tool_handlers={},
        memory_recall_policy=MemoryRecallPolicy("off"),
        completion_contract=CompletionContract("save_draft", lambda: CompletionDecision(status="needs_review", reason="READBACK_MISSING")))
    deltas = []; resets = []
    answer = agent.run("保存草稿", auto_select_skill=False, on_answer_delta=deltas.append,
        on_answer_reset=lambda: resets.append(True), is_cancelled=lambda: False)
    assert deltas == [answer] and resets == [True]
    assert "所有操作已经完成" not in answer
    assert agent.last_context_receipt.to_dict()["completion"] == agent.last_completion_receipt
    agent.completion_contract = None
    assert agent.run("普通问答", auto_select_skill=False) == "普通回答"
    assert agent.last_completion_receipt is None


def test_cancelled_next_run_does_not_expose_previous_completion():
    from agent.loop import AgentLoop
    from llm.client import LLMStreamCancelled
    from test_agent_loop import FakeLLMClient, FakeMessage
    agent = AgentLoop(FakeLLMClient([FakeMessage("第一次完成")]), trace=None,
        tool_definitions={}, tool_handlers={}, memory_recall_policy=MemoryRecallPolicy("off"),
        completion_contract=CompletionContract("save_candidate", lambda: CompletionDecision(
            status="completed", reason="VERIFIED", evidence={"artifact_id": "first-task"})))
    agent.run("第一次", auto_select_skill=False)
    assert agent.last_completion_receipt["status"] == "completed"
    with pytest.raises(LLMStreamCancelled):
        agent.run("第二次已经取消", auto_select_skill=False, is_cancelled=lambda: True)
    assert agent.last_completion_receipt is None
    assert agent.last_model_answer is None
    assert agent.last_context_receipt is None


@pytest.mark.parametrize("saved", [False, True])
def test_candidate_web_host_binds_real_persisted_artifact(production_env, saved):
    from fastapi.testclient import TestClient
    from uuid import uuid4
    from web_agent import app as web_app
    from tools.content_drafts import SAVE_DRAFT_TOOL
    env = production_env
    agents = []
    def factory(trace, scope):
        sdk = RecordedSDK([])
        script = [Reply(tool_calls=[ToolCall(id="candidate", name=SAVE_DRAFT_TOOL,
            arguments=json.dumps(env.tracker.payload, ensure_ascii=False))])] if saved else []
        sdk.script = iter([*script, Reply(content="任务已全部完成")])
        agent = RecordedAgent(RecordedLLM(LLMConfig("synthetic", "candidate"), sdk_client=sdk),
            trace=trace, tool_definitions={}, tool_handlers={}, memory_recall_policy=MemoryRecallPolicy("off"))
        agents.append(agent)
        return agent
    client = TestClient(web_app.create_app(factory, conversation_store=env.store))
    response = client.post(f"/api/conversations/{env.identifier}/content-production",
        json={"plan_version": 1, "content_id": "C01", "idempotency_key": str(uuid4())})
    assert (response.status_code == 200) is saved
    receipt = agents[0].last_completion_receipt
    assert receipt["task_type"] == "save_candidate"
    assert (receipt["status"] == "completed") is saved
    if saved:
        artifact = env.store.generation_ui_artifact(env.identifier, receipt["evidence"]["artifact_id"])
        assert artifact.data["result_ref"] == receipt["evidence"]["result_ref"]
        assert artifact.data["execution_status"] == "not_started"
        assert artifact.data["approval"] is None
