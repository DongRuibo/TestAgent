"""真实SQLite与Web接口的单条图文闭环；模型和平台为明确离线替身。"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tools import rpa_xiaohongshu
from tools.content_drafts import SAVE_DRAFT_TOOL
from web_agent import app as web_app
from web_agent.content_production import ContentProduction, ProductionRequest
from web_agent.content_workflow import ContentWorkflow
from web_agent.conversations import SQLiteConversationStore, GenerationUiActionConflictError


SOURCE = "https://www.xiaohongshu.com/explore/synthetic-note"


def candidate(**changes):
    return {"title": "小户型收纳先看动线", "body": "先观察动线，再决定收纳位置。", "tags": ["收纳"],
        "image_notes": "使用用户提供的现场照片；不假设照片中的具体柜体。", "source_refs": ["S01"],
        "claims": [{"text": "先观察动线，再决定收纳位置。", "source_refs": ["S01"]}],
        "missing_materials": [], **changes}


@pytest.fixture
def production_env(tmp_path, monkeypatch):
    root = tmp_path / "images"; root.mkdir()
    image = root / "case.png"; Image.new("RGB", (3, 3), "red").save(image)
    monkeypatch.setattr(rpa_xiaohongshu, "RPA_XHS_IMAGE_ROOT", root)
    store = SQLiteConversationStore(tmp_path / "chat.sqlite3")
    identifier = store.create()
    research = {"source_references": [{"source_id": "S01", "url": SOURCE}], "limitations": ["模拟样本"],
        "candidate_topics": [{"title": "收纳"}]}
    state = {"status": "plan_ready", "goal_ref": {"artifact_path": "synthetic-goal"},
        "weekly_inputs": {"plan_id": str(uuid4()), "staff_and_requirements": "用户已有一张照片"},
        "weekly_saved": {"version": 1, "markdown": "合成已保存计划", "content_ids": {"收纳": "C01", "动线": "C02"}},
        "research_calls": {"receipt01": {"tool": "social_get_post_detail", "params": {"url": SOURCE}, "status": "success",
            "result": {"success": True, "items": [{"url": SOURCE, "content": "先观察动线，再决定收纳位置。"}],
                "collected_at": "2026-11-05T08:00:00+08:00"}}}}
    store.save_content_workflow(identifier, state, expected_revision=0)
    workflow = SimpleNamespace(_goal=lambda state: {"company_name": "合成企业"}, _research=lambda state: deepcopy(research))
    tracker = SimpleNamespace(generations=0, executions=[], mode="normal", payload=candidate(), query_result=None)

    def generate(inputs, save):
        tracker.generations += 1
        tracker.inputs = inputs
        if tracker.mode == "no_save": return "未落盘回复"
        tracker.saved = save(**deepcopy(tracker.payload))
        if tracker.mode == "after_save": raise RuntimeError("合成保存后故障")

    def execute(**kwargs):
        tracker.executions.append(kwargs)
        kwargs["on_job_created"]("synthetic-job")
        if tracker.mode == "interrupt": raise RuntimeError("合成进程中断")
        return {"success": True, "status": "succeeded", "job_id": "synthetic-job", "message": "模拟平台完整核验通过", "final_publish_performed": False}

    def query(**kwargs):
        return tracker.query_result or {"success": False, "status": "needs_review", "needs_review": True,
            "job_id": kwargs.get("job_id"), "message": "模拟结果未确认"}

    service = ContentProduction(store, workflow, generate, execute, query)
    monkeypatch.setattr(ContentWorkflow, "_goal", lambda self, state: workflow._goal(state))
    monkeypatch.setattr(ContentWorkflow, "_research", lambda self, state: workflow._research(state))
    monkeypatch.setattr(web_app, "save_xiaohongshu_draft", execute)
    monkeypatch.setattr(web_app, "query_xiaohongshu_draft", query)

    class Agent:
        def run(self, task, **kwargs):
            assert set(self.tool_handlers) == {SAVE_DRAFT_TOOL}
            assert self.memory is None
            return generate({}, self.tool_handlers[SAVE_DRAFT_TOOL])

    client = TestClient(web_app.create_app(lambda *args: Agent(), conversation_store=store))
    return SimpleNamespace(store=store, identifier=identifier, service=service, tracker=tracker, image=image,
        client=client, state=state, research=research, tmp_path=tmp_path)


def produce(env, **changes):
    return env.service.produce(env.identifier, ProductionRequest(plan_version=1, content_id="C01",
        idempotency_key=uuid4(), **changes))


def action(env, data, name, **values):
    return env.service.action(env.identifier, data["artifact_id"], name,
        {"result_ref": data["result_ref"], "version": data["version"], **values}, str(uuid4()))


def editable_with_images(env, data):
    action(env, data, "edit_content_draft", title=data["title"], body=data["body"], tags_text="收纳",
        image_paths_text=str(env.image))
    return env.service.list(env.identifier)["drafts"][0]


def test_web_plan_to_generation_edit_approve_execute_and_refresh(production_env):
    env = production_env
    path = f"/api/conversations/{env.identifier}/content-production"
    response = env.client.post(path, json={"plan_version": 1, "content_id": "C01", "idempotency_key": str(uuid4())})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "pending_review" and env.tracker.generations == 1
    message = env.store.generation_ui_artifact(env.identifier, data["artifact_id"])
    event = {"conversation_id": env.identifier, "message_id": message.message_id, "artifact_id": message.artifact_id,
        "scenario": "content_draft", "event_type": "confirm", "action_id": "edit_content_draft",
        "arguments": {"version": 1, "result_ref": data["result_ref"], "title": data["title"], "body": data["body"],
            "tags_text": "收纳", "image_paths_text": str(env.image)}, "idempotency_key": str(uuid4())}
    edited = env.client.post("/api/generation/event", json=event)
    assert edited.status_code == 200, edited.text
    data = env.client.get(path).json()["drafts"][0]
    assert data["version"] == 2 and data["approval"] is None
    action(env, data, "review_content_draft", decision="approve")
    action(env, data, "save_content_draft")
    refreshed = env.client.get(f"/api/conversations/{env.identifier}")
    assert refreshed.status_code == 200
    assert env.service.list(env.identifier)["drafts"][0]["execution_status"] == "draft_saved"
    assert len(env.tracker.executions) == 1
    assert env.tracker.executions[0]["body"] == data["body"]


def test_generation_idempotency_and_saved_result_survives_model_error(production_env):
    env = production_env; env.tracker.mode = "after_save"
    request = ProductionRequest(plan_version=1, content_id="C01", idempotency_key=uuid4())
    saved = env.service.produce(env.identifier, request)
    assert env.service.produce(env.identifier, request) == saved
    assert env.tracker.generations == 1


@pytest.mark.parametrize("changes", [{"source_refs": ["FAKE"]}, {"body": ""}, {"claims": [{"text": "正文没有这句话", "source_refs": ["S01"]}]}])
def test_invalid_candidate_does_not_create_formal_draft(production_env, changes):
    env = production_env; env.tracker.payload = candidate(**changes)
    with pytest.raises(GenerationUiActionConflictError, match="未保存合法正文"):
        produce(env)
    assert env.service.list(env.identifier)["drafts"] == []


def test_missing_source_receipt_cannot_generate(production_env):
    env = production_env
    state = env.store.content_workflow(env.identifier)
    state["research_calls"] = {}
    env.store.save_content_workflow(env.identifier, state, expected_revision=state["revision"])
    with pytest.raises(GenerationUiActionConflictError, match="尚无已读取"):
        produce(env)
    assert env.tracker.generations == 0


def test_edit_invalidates_approval_and_old_version_cannot_execute(production_env):
    env = production_env; first = editable_with_images(env, produce(env))
    action(env, first, "review_content_draft", decision="approve")
    action(env, first, "edit_content_draft", title="新标题", body=first["body"], tags_text="收纳", image_paths_text=str(env.image))
    latest = env.service.list(env.identifier)["drafts"][0]
    assert latest["version"] == 3 and latest["approval"] is None
    with pytest.raises(GenerationUiActionConflictError, match="当前版本"):
        action(env, first, "save_content_draft")
    with pytest.raises(GenerationUiActionConflictError, match="只有已审核"):
        action(env, latest, "save_content_draft")
    assert not env.tracker.executions


def test_approved_image_replacement_is_rejected(production_env):
    env = production_env; data = editable_with_images(env, produce(env))
    action(env, data, "review_content_draft", decision="approve")
    Image.new("RGB", (3, 3), "blue").save(env.image)
    with pytest.raises(GenerationUiActionConflictError, match="图片内容已变化"):
        action(env, data, "save_content_draft")
    assert not env.tracker.executions


def test_missing_materials_can_be_explicitly_completed_in_new_version(production_env):
    env = production_env; env.tracker.payload = candidate(missing_materials=["现场照片"])
    data = produce(env)
    action(env, data, "edit_content_draft", title=data["title"], body=data["body"], tags_text="收纳",
        image_paths_text=str(env.image), materials_note="已上传用户确认的现场照片")
    data = env.service.list(env.identifier)["drafts"][0]
    assert data["missing_materials"] == [] and data["materials_note"]
    action(env, data, "review_content_draft", decision="approve")


def test_interrupted_execution_recovers_after_plan_revision_without_replay(production_env):
    env = production_env; data = editable_with_images(env, produce(env))
    action(env, data, "review_content_draft", decision="approve")
    env.tracker.mode = "interrupt"
    action(env, data, "save_content_draft")
    with pytest.raises(GenerationUiActionConflictError, match="已经创建执行"):
        action(env, data, "save_content_draft")
    state = env.store.content_workflow(env.identifier)
    state["weekly_saved"]["version"] = 2
    env.store.save_content_workflow(env.identifier, state, expected_revision=state["revision"])
    env.tracker.query_result = {"success": True, "status": "succeeded", "job_id": "synthetic-job", "message": "原任务核验成功"}
    action(env, data, "reconcile_content_draft")
    assert env.service.list(env.identifier)["drafts"][0]["execution_status"] == "draft_saved"
    assert len(env.tracker.executions) == 1


def test_cross_conversation_cannot_review_content(production_env):
    env = production_env; data = produce(env); other = env.store.create()
    with pytest.raises(GenerationUiActionConflictError, match="当前对话"):
        env.service.action(other, data["artifact_id"], "review_content_draft",
            {"version": data["version"], "result_ref": data["result_ref"], "decision": "approve"}, str(uuid4()))


@pytest.mark.parametrize("decision", ["found", "not_saved"])
def test_human_resolution_preserves_original_execution_and_requires_new_approval(production_env, decision):
    env = production_env; data = editable_with_images(env, produce(env))
    action(env, data, "review_content_draft", decision="approve")
    env.tracker.mode = "interrupt"
    action(env, data, "save_content_draft")
    action(env, data, "resolve_content_draft", decision=decision, note="已人工打开草稿箱核对本条标题与图片")
    latest = env.service.list(env.identifier)["drafts"][0]
    if decision == "found":
        assert latest["execution_status"] == "manually_confirmed"
        assert latest["execution_result"]["success"] is False
    else:
        assert latest["version"] == 3 and latest["approval"] is None
        assert latest["previous_execution_id"]
        with pytest.raises(GenerationUiActionConflictError, match="只有已审核"):
            action(env, latest, "save_content_draft")
        action(env, latest, "review_content_draft", decision="approve")
        env.tracker.mode = "normal"
        action(env, latest, "save_content_draft")
        assert len(env.tracker.executions) == 2
    with env.store._connect() as connection:
        rows = connection.execute("SELECT status,result_json FROM content_draft_executions ORDER BY rowid").fetchall()
    assert "human_resolution" in rows[0][1]


def test_source_receipts_keep_body_and_comments_and_generation_uses_original_text(production_env):
    from web_agent.research_session import source_receipts
    env = production_env
    state = env.store.content_workflow(env.identifier)
    state["research_calls"]["comments02"] = {"tool": "social_get_comments", "params": {"url": SOURCE}, "status": "success",
        "result": {"success": True, "items": [{"text": "合成评论：先确认通道宽度。"}], "collected_at": "2026-11-05T09:00:00+08:00"}}
    env.store.save_content_workflow(env.identifier, state, expected_revision=state["revision"])
    receipt = next(iter(source_receipts(state).values()))
    assert receipt["verification"] == "body_read" and receipt["receipt_ids"] == ["receipt01", "comments02"]
    produce(env)
    assert env.tracker.inputs["evidence"][0]["content"] == "先观察动线，再决定收纳位置。"
    assert env.tracker.inputs["evidence"][1]["comments"] == ["合成评论：先确认通道宽度。"]
    assert "research" not in env.tracker.inputs and "weekly_plan" not in env.tracker.inputs


@pytest.mark.parametrize("failure", ["connection", "cancelled"])
def test_web_failure_and_cancellation_keep_execution_receipt(production_env, failure):
    from llm.client import LLMConnectionError, LLMStreamCancelled
    env = production_env
    receipt = {"model_calls": [{"sdk_attempts": 3, "usage": None, "status": "failed"}], "tool_calls": []}

    class FailingAgent:
        generation_ui_artifacts = []; last_tool_results = []
        last_context_receipt = SimpleNamespace(to_dict=lambda: receipt)

        def run(self, task, **kwargs):
            raise LLMConnectionError("合成连接失败") if failure == "connection" else LLMStreamCancelled("合成取消")

    client = TestClient(web_app.create_app(lambda *args: FailingAgent(), conversation_store=env.store))
    if failure == "cancelled":
        response = client.post("/api/chat/stream", json={"conversation_id": env.identifier, "message": "普通问题", "request_id": str(uuid4())})
        assert response.status_code == 200 and '"cancelled"' in response.text
    else:
        response = client.post("/api/chat", json={"conversation_id": env.identifier, "message": "普通问题"})
        assert response.status_code == 503
    records = env.store.message_records(env.identifier)
    assert records[-1].context == receipt


def test_legacy_orphan_running_requires_human_review_then_new_approval(production_env):
    from tools.content_operations_ui import _spec, with_content_recovery
    env = production_env
    artifact_id = str(uuid4())
    ui = {"type": "generation_ui", "artifact_id": artifact_id, "scenario": "content_weekly_plan", "text": "合成旧卡片",
        "spec": with_content_recovery(_spec("content_weekly_plan", "旧计划", "合成", [], actions=[])),
        "data": {"contents": [{"content_id": "OLD01", "title": "旧标题", "body": "旧正文", "tags": [],
            "status": "approved", "image_paths": [str(env.image)], "rpa_status": "running"}], "metrics": {}}}
    message_id = env.store.append_assistant_response(env.identifier, "合成旧卡片", ui_artifacts=[ui])
    response = env.client.get(f"/api/conversations/{env.identifier}")
    assert response.status_code == 200
    current = env.store.generation_ui_artifact(env.identifier, artifact_id)
    assert current.data["contents"][0]["rpa_status"] == "needs_review"
    with pytest.raises(GenerationUiActionConflictError, match="先核对"):
        env.store.prepare_content_xiaohongshu_rpa(env.identifier, artifact_id,
            arguments={"content_id": "OLD01"}, idempotency_key=str(uuid4()))
    resolved = env.client.post("/api/generation/event", json={"conversation_id": env.identifier, "message_id": message_id,
        "artifact_id": artifact_id, "scenario": "content_weekly_plan", "event_type": "confirm",
        "action_id": "resolve_xiaohongshu_draft", "idempotency_key": str(uuid4()),
        "arguments": {"content_id": "OLD01", "resolution": "人工确认未保存", "note": "已人工打开草稿箱核对，不存在本条草稿"}})
    assert resolved.status_code == 200, resolved.text
    item = env.store.generation_ui_artifact(env.identifier, artifact_id).data["contents"][0]
    assert item["status"] == "pending_review" and item["rpa_history"][0]["rpa_status"] == "needs_review"
    assert not env.tracker.executions
    with pytest.raises(GenerationUiActionConflictError, match="只有已审核"):
        env.store.prepare_content_xiaohongshu_rpa(env.identifier, artifact_id,
            arguments={"content_id": "OLD01"}, idempotency_key=str(uuid4()))


def test_production_model_failure_preserves_receipt_without_claiming_draft(production_env):
    from llm.client import LLMConnectionError
    env = production_env
    receipt = {"model_calls": [{"sdk_attempts": 3, "usage": None, "status": "failed"}], "tool_calls": []}

    class FailedProductionAgent:
        last_context_receipt = SimpleNamespace(to_dict=lambda: receipt)
        def run(self, task, **options):
            raise LLMConnectionError("合成模型连接故障")

    client = TestClient(web_app.create_app(lambda *args: FailedProductionAgent(), conversation_store=env.store))
    response = client.post(f"/api/conversations/{env.identifier}/content-production",
        json={"plan_version": 1, "content_id": "C01", "idempotency_key": str(uuid4())})
    assert response.status_code == 502
    assert env.store.message_records(env.identifier)[-1].context == receipt
    assert env.service.list(env.identifier)["drafts"] == []
