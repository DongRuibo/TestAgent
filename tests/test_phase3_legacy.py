"""旧周计划入口也必须核验原执行，并保留历史幂等回执。"""

from copy import deepcopy
import json
from uuid import uuid4

import pytest
from PIL import Image

from test_content_operations import _weekly_plan_artifact
from test_content_production import verified_receipt
from web_agent.conversations import SQLiteConversationStore


@pytest.fixture
def legacy(tmp_path):
    _, artifact = _weekly_plan_artifact()
    store = SQLiteConversationStore(tmp_path / "legacy.db")
    identifier = store.create()
    store.append_assistant_response(identifier, "合成旧计划", ui_artifacts=[artifact])
    aid = store.generation_ui_artifacts(identifier)[0].artifact_id
    cid = artifact["data"]["contents"][0]["content_id"]
    image = tmp_path / "case.png"
    Image.new("RGB", (2, 2), "red").save(image)
    store.review_content_plan(identifier, aid, arguments={"content_id": cid, "decision": "确认内容",
        "image_paths": [str(image)], "note": "合成审核"}, idempotency_key=str(uuid4()))
    key = str(uuid4()); args = {"content_id": cid}
    prepared = store.prepare_content_xiaohongshu_rpa(identifier, aid, arguments=args, idempotency_key=key)
    store.attach_content_xiaohongshu_job(identifier, aid, cid, prepared["execution_id"], "synthetic-job")
    return store, identifier, aid, args, key, prepared


@pytest.mark.parametrize("fault", ["bare_success", "wrong_job", "wrong_body", "query_timeout", "unapproved", "valid"])
def test_legacy_success_is_verified_against_original_target(legacy, fault, monkeypatch):
    store, identifier, aid, args, key, prepared = legacy
    receipt = verified_receipt(prepared)
    if fault == "bare_success": receipt = {"success": True}
    if fault == "wrong_job": receipt["job_id"] = "other-job"
    if fault == "wrong_body":
        receipt["result"]["verification"]["fields"]["body"]["actual"] = "其他正文"
    if fault == "unapproved":
        with store._connect() as db:
            data = store.generation_ui_artifact(identifier, aid).data
            data["contents"][0]["status"] = "pending_review"
            db.execute("UPDATE generation_ui_artifacts SET data_json=? WHERE artifact_id=?", (json.dumps(data), aid))
    calls = []
    def query(**kwargs):
        calls.append(kwargs)
        if fault == "query_timeout": raise TimeoutError("合成核验超时")
        return deepcopy(receipt)
    monkeypatch.setattr("tools.rpa_xiaohongshu.query_xiaohongshu_draft", query)
    response = store.finish_content_xiaohongshu_rpa(identifier, aid, arguments=args, idempotency_key=key,
        rpa_result={"success": True})
    assert response["status"] == ("draft_saved" if fault == "valid" else "needs_review")
    assert calls == [{"execution_id": prepared["execution_id"], "job_id": "synthetic-job"}]
    assert store.generation_ui_artifact(identifier, aid).data["contents"][0]["rpa_job_id"] == "synthetic-job"


def test_legacy_reconciliation_preserves_original_receipt_and_manual_terminal(legacy, monkeypatch):
    store, identifier, aid, args, key, prepared = legacy
    first = store.finish_content_xiaohongshu_rpa(identifier, aid, arguments=args, idempotency_key=key,
        rpa_result={"success": False, "status": "needs_review", "needs_review": True})
    with store._connect() as db:
        original = db.execute("SELECT response_json FROM generation_ui_actions WHERE idempotency_key=?", (key,)).fetchone()[0]
    monkeypatch.setattr("tools.rpa_xiaohongshu.query_xiaohongshu_draft", lambda **_: verified_receipt(prepared))
    store.finish_content_xiaohongshu_rpa(identifier, aid, arguments=args, idempotency_key=key,
        rpa_result={"success": True}, reconcile=True)
    replay = store.prepare_content_xiaohongshu_rpa(identifier, aid, arguments=args, idempotency_key=key)["existing_response"]
    assert replay["status"] == first["status"] == "needs_review"
    assert replay["replayed"] is True and replay["current_business_status"] == "draft_saved"
    assert replay["current_data_patch"]["contents"][0]["rpa_status"] == "draft_saved"
    with store._connect() as db:
        assert db.execute("SELECT response_json FROM generation_ui_actions WHERE idempotency_key=?", (key,)).fetchone()[0] == original
        data = store.generation_ui_artifact(identifier, aid).data
        data["contents"][0]["rpa_status"] = "manually_confirmed"
        db.execute("UPDATE generation_ui_artifacts SET data_json=? WHERE artifact_id=?", (json.dumps(data), aid))
    monkeypatch.setattr("tools.rpa_xiaohongshu.query_xiaohongshu_draft", lambda **_: pytest.fail("人工终态不查询"))
    final = store.finish_content_xiaohongshu_rpa(identifier, aid, arguments=args, idempotency_key=key,
        rpa_result={"success": True}, reconcile=True)
    assert final["status"] == "manually_confirmed"
