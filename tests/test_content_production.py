"""真实SQLite与Web接口的单条图文闭环；模型和平台为明确离线替身。"""

from copy import deepcopy
import json
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


def verified_receipt(post):
    """成功替身也提供生产适配器要求的完整读回证据，不再只返回布尔值。"""
    tags = ["#" + tag for tag in post["tags"]]
    values = {"title": post["title"], "body": post["body"] + ("\n\n" + " ".join(tags) if tags else ""),
        "image_count": len(post["image_paths"]), "tags": tags}
    result = {"result_type": "xiaohongshu_draft_saved", "final_publish_performed": False,
        "verification": {"draft_reopened": True, "fields": {
            name: {"expected": value, "actual": value, "matched": True} for name, value in values.items()}}}
    return rpa_xiaohongshu._draft_job_result(SimpleNamespace(
        id="synthetic-job", status="succeeded", input=post, result=result))


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
    tracker = SimpleNamespace(generations=0, executions=[], mode="normal", payload=candidate(), query_result=None, completed_result=None)

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
        tracker.completed_result = verified_receipt(kwargs)
        return tracker.completed_result

    def query(**kwargs):
        return tracker.query_result or tracker.completed_result or {"success": False, "status": "needs_review", "needs_review": True,
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
    env.tracker.query_result = verified_receipt(data)
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


@pytest.mark.parametrize("machine_result", [
    {"success": False, "status": "needs_review"},
    {"success": False, "status": "running"},
    {"success": True, "status": "succeeded"},
])
def test_explicit_reconcile_preserves_manual_confirmation_without_query(production_env, machine_result):
    env = production_env
    data = editable_with_images(env, produce(env))
    action(env, data, "review_content_draft", decision="approve")
    env.tracker.mode = "interrupt"
    action(env, data, "save_content_draft")
    action(env, data, "resolve_content_draft", decision="found", note="已人工确认原草稿存在")
    before = env.store.generation_ui_artifact(env.identifier, data["artifact_id"]).data
    with env.store._connect() as connection:
        execution_before = connection.execute("SELECT status,result_json FROM content_draft_executions").fetchone()
    queries = []
    env.service.query_draft = lambda **arguments: queries.append(arguments) or machine_result
    arguments = {"result_ref": data["result_ref"], "version": data["version"]}
    key = str(uuid4())
    for request_key in (key, key, str(uuid4())):
        response = env.service.action(env.identifier, data["artifact_id"], "reconcile_content_draft", arguments, request_key)
        assert response["status"] == "manually_confirmed"
        assert response["data_patch"] == before
        assert "不代表机器完成完整字段核验" in response["message"]
    assert not queries
    assert env.store.generation_ui_artifact(env.identifier, data["artifact_id"]).data == before
    with env.store._connect() as connection:
        assert connection.execute("SELECT status,result_json FROM content_draft_executions").fetchone() == execution_before
        assert json.loads(connection.execute("SELECT data_json FROM content_draft_versions WHERE artifact_id=?",
            (data["artifact_id"],)).fetchone()[0]) == before
    assert len(env.tracker.executions) == 1


def test_explicit_reconcile_rejects_old_version_after_not_saved_resolution(production_env):
    env = production_env
    data = editable_with_images(env, produce(env))
    action(env, data, "review_content_draft", decision="approve")
    env.tracker.mode = "interrupt"
    action(env, data, "save_content_draft")
    action(env, data, "resolve_content_draft", decision="not_saved", note="已人工确认没有保存")
    before = env.service.list(env.identifier)["drafts"][0]
    queries = []
    env.service.query_draft = lambda **arguments: queries.append(arguments) or {"success": True}
    with pytest.raises(GenerationUiActionConflictError, match="当前版本"):
        action(env, data, "reconcile_content_draft")
    assert not queries
    assert env.service.list(env.identifier)["drafts"][0] == before
    assert before["execution_status"] == "not_started" and before["approval"] is None
    with env.store._connect() as connection:
        assert connection.execute("SELECT status FROM content_draft_executions").fetchone()[0] == "reviewed_not_saved"
    assert len(env.tracker.executions) == 1


@pytest.mark.parametrize("pending_status", ["running", "needs_review"])
def test_explicit_reconcile_still_recovers_pending_execution(production_env, pending_status):
    env = production_env
    data = editable_with_images(env, produce(env))
    action(env, data, "review_content_draft", decision="approve")
    def pending(**arguments):
        env.tracker.executions.append(arguments)
        arguments["on_job_created"]("synthetic-job")
        return {"success": False, "status": pending_status, "job_id": "synthetic-job"}
    env.service.save_draft = pending
    assert action(env, data, "save_content_draft")["status"] == pending_status
    queries = []
    env.service.query_draft = lambda **arguments: queries.append(arguments) or verified_receipt(data)
    response = action(env, data, "reconcile_content_draft")
    assert response["status"] == "draft_saved"
    assert len(queries) == 1 and len(env.tracker.executions) == 1
    with env.store._connect() as connection:
        assert connection.execute("SELECT status FROM content_draft_executions").fetchone()[0] == "draft_saved"


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


def test_source_receipt_confusion_and_nonverbatim_claim_can_be_locally_repaired(production_env):
    env = production_env
    receipts = []
    original_workflow = deepcopy(env.store.content_workflow(env.identifier))
    def generate(inputs, save):
        assert inputs["allowed_source_ids"] == ["S01"]
        assert "receipt_id" in inputs["boundary"] and "逐字复制" in inputs["boundary"]
        assert "不证明已经收到或授权" in inputs["boundary"]
        receipts.append(save(**candidate(source_refs=["S01:receipt01"],
            claims=[{"text": "先观察动线，再决定收纳位置。", "source_refs": ["S01:receipt01"]}])))
        assert receipts[-1]["success"] is False
        assert receipts[-1]["allowed_source_ids"] == ["S01"]
        receipts.append(save(**candidate(claims=[{"text": "应先看动线，随后安排收纳。", "source_refs": ["S01"]}])))
        assert receipts[-1]["success"] is False
        assert "原句" in receipts[-1]["message"] and "逐字复制" in receipts[-1]["claim_text_rule"]
        repaired = candidate(missing_materials=["照片收到情况和使用授权尚待人工核实"])
        receipts.append(save(**repaired))
        assert receipts[-1]["success"] is True
    env.service.generate = generate
    saved = produce(env)
    assert saved["version"] == 1 and saved["status"] == "pending_review"
    assert saved["body"] == candidate()["body"] and saved["source_refs"] == ["S01"]
    assert saved["approval"] is None and saved["execution_status"] == "not_started" and saved["image_paths"] == []
    assert env.store.content_workflow(env.identifier) == original_workflow
    assert len(env.service.list(env.identifier)["drafts"]) == 1 and not env.tracker.executions


def test_failed_local_repairs_keep_existing_approved_other_draft_and_original_versions(production_env):
    env = production_env
    first = produce(env)
    other = env.service.produce(env.identifier, ProductionRequest(plan_version=1, content_id="C02", idempotency_key=uuid4()))
    action(env, other, "edit_content_draft", title=other["title"], body=other["body"], tags_text="收纳", image_paths_text=str(env.image))
    other = next(draft for draft in env.service.list(env.identifier)["drafts"] if draft["draft_id"] == other["draft_id"])
    action(env, other, "review_content_draft", decision="approve")
    before = deepcopy(next(draft for draft in env.service.list(env.identifier)["drafts"] if draft["draft_id"] == other["draft_id"]))
    receipts = []
    def generate(inputs, save):
        receipts.append(save(**candidate(source_refs=["receipt01"])))
        receipts.append(save(**candidate(claims=[{"text": "来源原文不等于正文原句", "source_refs": ["S01"]}])))
        receipts.append(save(**candidate(title="第一条仅修订表达")))
    env.service.generate = generate
    revised = produce(env, expected_version=1)
    assert [result["success"] for result in receipts] == [False, False, True]
    assert revised["version"] == 2 and revised["approval"] is None
    after = next(draft for draft in env.service.list(env.identifier)["drafts"] if draft["draft_id"] == other["draft_id"])
    assert after == before and after["approval"] is not None
    with env.store._connect() as connection:
        old = connection.execute("SELECT data_json FROM content_draft_versions WHERE draft_id=? AND version=1", (first["draft_id"],)).fetchone()
    import json
    old = json.loads(old[0])
    assert old["body"] == first["body"] and old["source_refs"] == first["source_refs"] and old["status"] == "superseded"
    assert not env.tracker.executions


def test_draft_schema_explains_source_ids_claim_verbatim_and_unverified_images():
    from tools.content_drafts import DRAFT_TOOL_DEFINITION
    schema = DRAFT_TOOL_DEFINITION["function"]["parameters"]
    assert "allowed_source_ids" in schema["properties"]["source_refs"]["description"]
    assert "receipt_id" in schema["properties"]["source_refs"]["description"]
    assert "逐字" in schema["$defs"]["DraftClaim"]["properties"]["text"]["description"]
    assert "不证明已经收到或授权" in schema["properties"]["image_notes"]["description"]
    assert "授权" in schema["properties"]["missing_materials"]["description"]
