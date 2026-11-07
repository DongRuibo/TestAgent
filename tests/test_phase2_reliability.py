"""第二轮使用真实宿主、Job仓库和隔离平台替身检查完成证据。"""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from evals.content_runner import BusinessFixture
from evals.agent_runner import RecordedSDK, RecordedAgent, RecordedLLM, Reply, ToolCall
from evals.reliability_runner import isolated, simulated_robot, snapshot
from agent.context import MemoryRecallPolicy
from llm.client import LLMConfig
from tools import rpa_xiaohongshu
from tools.content_drafts import DRAFT_TOOL_DEFINITION, SAVE_DRAFT_TOOL
from web_agent import app as web_app
from web_agent.conversations import GenerationUiActionConflictError


@pytest.fixture
def phase2_env(tmp_path):
    with isolated(tmp_path), simulated_robot(tmp_path, []):
        env = BusinessFixture(tmp_path, "generate", [])
        env.service.save_draft = rpa_xiaohongshu.save_xiaohongshu_draft
        yield env


@pytest.mark.parametrize("claimed", [{"success": True}, {"success": "yes"}, None])
def test_save_claim_without_original_job_cannot_complete(phase2_env, claimed):
    env = phase2_env
    data = env.approve()
    env.service.save_draft = lambda **_: claimed
    response = env.action(data, "save_content_draft")
    evidence = snapshot(env)
    assert response["status"] == "needs_review"
    assert env.latest()["execution_status"] == "needs_review"
    assert evidence["executions"][0]["status"] == "needs_review"
    assert evidence["jobs"] == evidence["effects"] == []


@pytest.mark.parametrize("fault", ["bare_success", "body_mismatch", "wrong_job", "publish", "readback_failed", "query_timeout"])
def test_success_requires_complete_readback_for_original_target(phase2_env, fault):
    env = phase2_env
    data = env.approve()
    original = env.service.query_draft

    def faulty_query(**arguments):
        receipt = deepcopy(original(**arguments))
        assert receipt["success"] is True
        if fault == "bare_success":
            return {"success": True}
        if fault == "body_mismatch":
            field = receipt["result"]["verification"]["fields"]["body"]
            field.update(expected="另一个目标的正文", actual="另一个目标的正文")
            receipt["verification"] = deepcopy(receipt["result"]["verification"])
        elif fault == "wrong_job":
            receipt["job_id"] = "unrelated-job"
        elif fault == "publish":
            receipt["final_publish_performed"] = True
        elif fault == "readback_failed":
            return {"success": False, "status": "needs_review", "job_id": receipt["job_id"]}
        elif fault == "query_timeout":
            raise TimeoutError("合成读回查询超时")
        return receipt

    env.service.query_draft = faulty_query
    response = env.action(data, "save_content_draft")
    evidence = snapshot(env)
    assert response["status"] == env.latest()["execution_status"] == "needs_review"
    assert evidence["executions"][0]["status"] == "needs_review"
    assert len(evidence["effects"]) == len(evidence["jobs"]) == 1
    assert evidence["effects"][0]["published"] is False


@pytest.mark.parametrize("action", ["explicit", "automatic"])
def test_reconcile_rejects_incomplete_success_receipt(phase2_env, action):
    env = phase2_env
    data = env.approve()
    env.service.save_draft = lambda **_: {"success": False, "status": "needs_review"}
    env.action(data, "save_content_draft")
    env.service.query_draft = lambda **_: {"success": True}
    if action == "explicit":
        env.action(data, "reconcile_content_draft")
    else:
        env.service.reconcile(env.identifier)
    assert env.latest()["execution_status"] == "needs_review"
    evidence = snapshot(env)
    assert evidence["executions"][0]["status"] == "needs_review"
    assert evidence["effects"] == evidence["jobs"] == []


def test_complete_original_job_remains_successful(phase2_env):
    env = phase2_env
    data = env.approve()
    response = env.action(data, "save_content_draft")
    evidence = snapshot(env)
    assert response["status"] == "draft_saved"
    assert evidence["executions"][0]["execution_id"] == evidence["jobs"][0]["execution_id"] == evidence["effects"][0]["execution_id"]
    assert evidence["executions"][0]["job_id"] == evidence["jobs"][0]["id"] == evidence["effects"][0]["job_id"]
    assert len(evidence["effects"]) == 1


def test_incomplete_transport_receipt_can_recover_from_original_job(phase2_env):
    env = phase2_env
    data = env.approve()
    def incomplete_transport(**arguments):
        rpa_xiaohongshu.save_xiaohongshu_draft(**{**arguments, "on_job_created": None})
        return {"success": True}
    env.service.save_draft = incomplete_transport
    assert env.action(data, "save_content_draft")["status"] == "draft_saved"
    evidence = snapshot(env)
    assert len(evidence["effects"]) == len(evidence["jobs"]) == 1
    assert evidence["executions"][0]["job_id"] == evidence["jobs"][0]["id"]


def test_failure_receipt_cannot_replace_persisted_job_link(phase2_env):
    env = phase2_env
    data = env.approve()
    env.service.query_draft = lambda **_: {"success": False, "status": "needs_review", "job_id": "unrelated-job"}
    assert env.action(data, "save_content_draft")["status"] == "needs_review"
    evidence = snapshot(env)
    assert evidence["executions"][0]["job_id"] == evidence["jobs"][0]["id"]
    assert len(evidence["effects"]) == 1


@pytest.mark.parametrize("scenario", ["no_tools", "natural_language", "failed_tool", "incomplete_ack",
    "readback_failed", "waiting_approval", "model_false_after_save"])
def test_business_host_uses_evidence_instead_of_model_completion_claim(phase2_env, scenario):
    env = phase2_env
    claims = []
    def generate(inputs, save):
        sdk = RecordedSDK(env.events)
        script = []
        payload = deepcopy(env.payload)
        if scenario == "failed_tool":
            payload["body"] = ""
        handler = (lambda **_: {"success": True}) if scenario == "incomplete_ack" else save
        if scenario not in {"no_tools", "natural_language"}:
            script.append(Reply(tool_calls=[ToolCall(id="candidate", name=SAVE_DRAFT_TOOL,
                arguments=json.dumps(payload, ensure_ascii=False))]))
        final = "任务已经全部完成。" if scenario == "natural_language" else json.dumps({"completed": scenario != "model_false_after_save"})
        script.append(Reply(content=final))
        sdk.script = iter(script)
        agent = RecordedAgent(RecordedLLM(LLMConfig("synthetic", "scripted-host-boundary"), sdk_client=sdk),
            trace=None, tool_definitions={SAVE_DRAFT_TOOL: DRAFT_TOOL_DEFINITION}, tool_handlers={SAVE_DRAFT_TOOL: handler},
            tool_budgets={SAVE_DRAFT_TOOL: 2}, memory_recall_policy=MemoryRecallPolicy("off"))
        claims.append(agent.run("制作并保存正文候选；正式状态由宿主核验。", auto_select_skill=False))
    env.service.generate = generate
    if scenario in {"no_tools", "natural_language", "failed_tool", "incomplete_ack"}:
        with pytest.raises(GenerationUiActionConflictError, match="未保存合法正文"):
            env.produce()
        assert snapshot(env)["drafts"] == []
    else:
        data = env.produce()
        assert data["status"] == "pending_review" and data["execution_status"] == "not_started"
        if scenario == "waiting_approval":
            with pytest.raises(GenerationUiActionConflictError, match="只有已审核"):
                env.action(data, "save_content_draft")
        elif scenario == "readback_failed":
            data = env.edit(data)
            env.action(data, "review_content_draft", decision="approve")
            env.service.query_draft = lambda **_: {"success": False, "status": "needs_review"}
            assert env.action(data, "save_content_draft")["status"] == "needs_review"
        else:
            assert claims == ['{"completed": false}']
            assert len(snapshot(env)["drafts"]) == 1
    assert claims
    assert len(snapshot(env)["effects"]) == (1 if scenario == "readback_failed" else 0)


@pytest.fixture
def replayed_env(phase2_env):
    env = phase2_env
    data = env.approve()
    def lose_response(**arguments):
        rpa_xiaohongshu.save_xiaohongshu_draft(**arguments)
        raise TimeoutError("合成保存成功但响应丢失")
    env.service.save_draft = lose_response
    key = str(uuid4())
    original = env.action(data, "save_content_draft", key=key)
    assert original["status"] == "needs_review"
    with env.store._connect() as connection:
        stored = connection.execute("SELECT response_json FROM generation_ui_actions WHERE idempotency_key=?", (key,)).fetchone()[0]
    assert env.action(data, "reconcile_content_draft")["status"] == "draft_saved"
    return env, data, key, original, stored


def test_replay_separates_historical_response_and_current_state(replayed_env):
    env, data, key, original, stored = replayed_env
    replay = env.action(data, "save_content_draft", key=key)
    assert replay["status"] == original["status"] == "needs_review"
    assert replay["data_patch"] == original["data_patch"]
    assert replay["replayed"] is True
    assert replay["original_execution_status"] == "needs_review"
    assert replay["current_business_status"] == "draft_saved"
    assert replay["current_data_patch"] == env.latest()
    with env.store._connect() as connection:
        assert connection.execute("SELECT response_json FROM generation_ui_actions WHERE idempotency_key=?", (key,)).fetchone()[0] == stored
    assert len(snapshot(env)["effects"]) == len(snapshot(env)["jobs"]) == 1


def test_web_replay_response_preserves_additive_fields(replayed_env):
    env, data, key, original, _ = replayed_env
    artifact = env.store.generation_ui_artifact(env.identifier, data["artifact_id"])
    client = TestClient(web_app.create_app(lambda *args: pytest.fail("重放不得调用模型"), conversation_store=env.store))
    response = client.post("/api/generation/event", json={"conversation_id": env.identifier,
        "message_id": artifact.message_id, "artifact_id": artifact.artifact_id, "scenario": "content_draft",
        "event_type": "confirm", "action_id": "save_content_draft", "idempotency_key": key,
        "arguments": {"version": data["version"], "result_ref": data["result_ref"]}})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == original["status"]
    assert payload["replayed"] is True and payload["original_execution_status"] == "needs_review"
    assert payload["current_business_status"] == "draft_saved"
    assert payload["current_data_patch"]["execution_status"] == "draft_saved"
    assert len(snapshot(env)["effects"]) == 1


@pytest.mark.parametrize("replay", [False, True])
def test_web_action_callback_prefers_current_patch_only_for_replay(replay):
    source = (Path(__file__).resolve().parents[1] / "apps/dev-agent/src/web_agent/static/app.js").read_text("utf-8")
    function = source[source.index("async function submitGenerationUiEvent("):source.index("async function readNdjson(")]
    payload = {"status": "needs_review", "message": "原执行中断", "data_patch": {"execution_status": "needs_review"}}
    if replay:
        payload.update(replayed=True, original_execution_status="needs_review", current_business_status="draft_saved",
            current_data_patch={"execution_status": "draft_saved"})
    script = """
const payload = JSON.parse(process.argv[1]);
const generationUiActionRequests = new Map();
const notices = [];
const host = {};
const document = {querySelector: () => host};
const crypto = {randomUUID: () => 'synthetic-key'};
const fetch = async () => ({ok: true, json: async () => payload});
const contentWorkflow = {refresh: () => {}};
const showInlineGenerationUiNotice = (_, message) => notices.push(message);
""" + function + """
submitGenerationUiEvent({artifact_id:'synthetic-artifact',scenario:'content_draft',conversation_id:'synthetic-conversation',message_id:1},
 {actionId:'save_content_draft',action:{event_type:'confirm'}}, {}).then(result => {
 process.stdout.write(JSON.stringify({result,notices}));
}).catch(error => {process.stderr.write(String(error));process.exitCode=1;});
"""
    completed = subprocess.run(["node", "-e", script, json.dumps(payload)], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    if replay:
        assert observed["result"]["data_patch"]["execution_status"] == "draft_saved"
        assert observed["result"]["original_execution_status"] == "needs_review"
        assert "草稿已保存" in observed["notices"][0]
    else:
        assert observed["result"] == payload
