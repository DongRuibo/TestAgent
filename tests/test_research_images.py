"""图片输入、可信回执、预算与多轮恢复；不访问外部服务。"""
import json
from types import SimpleNamespace

import pytest

from test_content_workflow import workflow_env, bind
from test_content_research import _valid_result
from tools.content_research import ContentResearchTools
from tools.research_images import IMAGE_TOOL_NAME, VisualReceipt, analyze_images, image_urls
from web_agent.content_workflow import ContentWorkflow, WorkflowAction
from web_agent.research_session import normalized_request, research_limits, visual_receipts
from web_agent.business_text import research_text


URL = "https://www.xiaohongshu.com/explore/test-vision"
POST = {"url": URL, "image_urls": ["https://sns-i11.rednotecdn.com/public1", "https://sns-i11.rednotecdn.com/public2"]}
ANALYSIS = {"images": [{"image_index": 1, "visible": "浅色柜门和开放格", "readable_text": "",
    "inference": "可能为渲染图", "uncertainty": "不能确认材质和真实尺寸"}],
    "limitations": ["只读取一张图片；不能证明互动原因"]}


def test_vision_sends_actual_image_and_separates_observation():
    requests = []
    def complete(messages):
        requests.append(messages)
        return json.dumps(ANALYSIS, ensure_ascii=False)
    result = analyze_images(POST, [1], complete=complete)
    assert result["success"] and result["analysis"] == ANALYSIS
    assert requests[0][1]["content"][-1]["image_url"]["url"] == POST["image_urls"][0]
    assert "likes" not in json.dumps(requests)


@pytest.mark.parametrize("url", ["http://sns-i11.rednotecdn.com/image", "https://127.0.0.1/image",
    "https://rednotecdn.com.evil.invalid/image", "file:///tmp/image", "https://u:p@sns-i11.rednotecdn.com/a"])
def test_reject_arbitrary_or_non_https_image(url):
    with pytest.raises(ValueError):
        image_urls({"image_urls": [url]}, [1])


@pytest.mark.parametrize("indexes", [[1, 1], [1, 2, 3, 4], [True], [], [999], [0]])
def test_invalid_selection(indexes):
    with pytest.raises(ValueError):
        image_urls(POST, indexes)


def test_failed_model_is_not_retried_or_reported_as_seen():
    calls = []
    def fail(messages):
        calls.append(messages)
        raise RuntimeError("secret-url-token")
    result = analyze_images(POST, [1], complete=fail)
    assert len(calls) == 1 and not result["success"] and "secret" not in str(result)
    assert not analyze_images(POST, [2], complete=lambda _: json.dumps(ANALYSIS))["success"]
    assert not analyze_images(POST, [1], complete=lambda _: '{"error":"unreadable"}')["success"]


def test_request_cache_normalizes_order_and_signed_post_links():
    a = normalized_request(IMAGE_TOOL_NAME, (), {"url": URL + "?xsec_token=synthetic", "image_indexes": [2, 1]})
    b = normalized_request(IMAGE_TOOL_NAME, (), {"url": URL, "image_indexes": [1, 2]})
    assert a == b
    assert research_limits("不用分析图片")[IMAGE_TOOL_NAME] == 0
    assert research_limits("图片分析最多1篇")[IMAGE_TOOL_NAME] == 1


def test_host_binds_cached_detail_and_persists_receipts(workflow_env, monkeypatch):
    env = workflow_env
    bind(env)
    visual_calls, results = [], []
    def inspect(post, indexes):
        visual_calls.append((post, indexes))
        return {"success": True, "analysis": ANALYSIS, "message": "已分析", "analyzed_at": "2026-09-18T03:00:00+00:00"}
    monkeypatch.setattr("web_agent.content_workflow.analyze_images", inspect)
    def run(identifier, path, configure):
        agent = SimpleNamespace(tool_handlers={"social_get_post_detail": lambda **kw: {"success": True, "items": [POST]},
            "save_content_research_result": lambda **kw: {"success": False}})
        configure(agent)
        agent.tool_handlers["social_get_post_detail"](url=URL)
        results.append(agent.tool_handlers[IMAGE_TOOL_NAME](url=URL, image_indexes=[1]))
        results.append(agent.tool_handlers[IMAGE_TOOL_NAME](url=URL, image_indexes=[1]))
        results.append(agent.tool_handlers[IMAGE_TOOL_NAME](url=URL, image_indexes=[2]))
        results.append(agent.tool_handlers[IMAGE_TOOL_NAME](url=URL + "other", image_indexes=[1]))
    workflow = ContentWorkflow(env.store, run)
    before = env.store.content_workflow(env.identifier)
    state = workflow.act(env.identifier, WorkflowAction(action="research", expected_revision=before["revision"], confirm_research=True))
    assert len(visual_calls) == 1 and results[0] == results[1]
    assert not results[2]["success"] and results[3]["error_type"] == "RESEARCH_BUDGET_EXCEEDED"
    receipts = visual_receipts(state)
    assert receipts[0]["analysis"] == ANALYSIS
    state = workflow.act(env.identifier, WorkflowAction(action="continue_research", expected_revision=state["revision"]))
    assert len(visual_calls) == 1
    saved = []
    def save(**payload):
        saved.append(payload)
        return {"success": False}
    state, result = workflow._save_research(env.identifier, state, {"visual_analyses": [{"status": "success"}]}, save)
    assert not result["success"] and not saved
    data = _valid_result()
    data["source_references"][0]["url"] = URL
    data["candidate_topics"][0]["visual_borrowing"] = "参考整体展示方式，具体素材待核实"
    state, result = workflow._save_research(env.identifier, state, data, save)
    assert saved[0]["visual_analyses"] == receipts
    assert saved[0]["execution_status"] == "partial_success"
    assert saved[0]["source_references"][-1]["source_id"] == receipts[0]["analysis_id"]
    assert saved[0]["source_references"][-1]["url"] == URL
    assert saved[0]["candidate_topics"][0]["visual_refs"] == [receipts[0]["analysis_id"]]


def test_no_vision_claim_without_successful_receipt(tmp_path):
    payload = _valid_result()
    topic = payload["candidate_topics"][0]
    topic["visual_refs"] = ["invented"]
    result = ContentResearchTools(output_dir=tmp_path).save_result(**payload)
    assert not result["success"]
    topic["visual_refs"] = []
    topic["visual_borrowing"] = "借鉴配图顺序"
    assert not ContentResearchTools(output_dir=tmp_path).save_result(**payload)["success"]


def test_visual_detail_readable_and_old_results_still_render():
    data = _valid_result()
    assert "未分析图片" in research_text(data)
    data["visual_analyses"] = [{"analysis_id": "v1", "status": "success", "image_indexes": [1],
        "analysis": ANALYSIS, "message": "已分析"}]
    data["candidate_topics"][0].update(visual_refs=["v1"], visual_borrowing="从整体到细节讲解")
    assert "1 篇、1 张" in research_text(data)
    text = research_text(data, topic_number=1)
    assert "浅色柜门" in text and "推测：可能为渲染图" in text and "从整体到细节" in text


def test_inference_requires_uncertainty(tmp_path):
    data = _valid_result()
    item = data["candidate_topics"][0]["evidence"][0]
    item["kind"] = "inference"
    assert not ContentResearchTools(output_dir=tmp_path).save_result(**data)["success"]
    item["uncertainty"] = "只有少量样本，尚待验证"
    assert ContentResearchTools(output_dir=tmp_path).save_result(**data)["success"]


def test_causal_overstatement_rejected_in_topic_fact(workflow_env):
    env = workflow_env
    bind(env)
    workflow = ContentWorkflow(env.store, lambda *a: None)
    state = env.store.content_workflow(env.identifier)
    data = _valid_result()
    data["candidate_topics"][0]["evidence"][0]["fact"] = "样本互动最高的图文依靠尺寸图。"
    from test_content_research import synthetic_detail
    url = data["source_references"][0]["url"]
    state = workflow._save(env.identifier, state, research_calls={"synthetic-detail": {"tool": "social_get_post_detail",
        "params": {"url": url}, "status": "success", "result": synthetic_detail(url)}})
    state, result = workflow._save_research(env.identifier, state, data, lambda **kw: pytest.fail("must not save"))
    assert not result["success"] and result["validation_errors"][0]["field"].endswith("evidence.0")


def test_interrupted_visual_query_never_creates_another_model_call(workflow_env, monkeypatch):
    env = workflow_env
    bind(env)
    workflow = ContentWorkflow(env.store, lambda *a: None)
    state = env.store.content_workflow(env.identifier)
    params, key = normalized_request(IMAGE_TOOL_NAME, (), {"url": URL, "image_indexes": [1]})
    state = workflow._save(env.identifier, state, status="failed", research_calls={key: {
        "tool": IMAGE_TOOL_NAME, "params": params, "status": "inflight"}})
    monkeypatch.setattr("web_agent.content_workflow.analyze_images", lambda *a: pytest.fail("must not run"))
    state = workflow.act(env.identifier, WorkflowAction(action="query_research", expected_revision=state["revision"]))
    assert state["research_calls"][key]["status"] == "failed"
    assert visual_receipts(state)[0]["analysis"] is None


def test_visual_receipt_and_source_must_match(tmp_path):
    data = _valid_result()
    receipt = {"analysis_id": "v1", "post_url": URL, "status": "success", "image_indexes": [1],
        "analyzed_at": "2026-09-18T03:00:00+00:00", "analysis": ANALYSIS, "message": "已分析"}
    with pytest.raises(ValueError):
        VisualReceipt.model_validate({**receipt, "analysis": None})
    with pytest.raises(ValueError):
        VisualReceipt.model_validate({**receipt, "status": "failed"})
    data["visual_analyses"] = [receipt]
    data["candidate_topics"][0]["visual_refs"] = ["v1"]
    saver = ContentResearchTools(output_dir=tmp_path)
    assert not saver.save_result(**data)["success"]
    data["source_references"][0]["url"] = URL
    assert saver.save_result(**data)["success"]


@pytest.mark.parametrize("notes, count", [("小周周二给三张衣柜照片，不补拍。", "三张照片"),
    ("至少3张小周的照片，最终收到再看。", "至少3张照片")])
def test_research_context_keeps_photo_count_without_staff_names(workflow_env, notes, count):
    workflow = ContentWorkflow(workflow_env.store, lambda *a: None)
    context = workflow.weekly.research_context({"weekly_inputs": {
        "start": "2026-09-21", "end": "2026-09-27",
        "staff_and_requirements": notes}})
    assert count in context and "小周" not in context


def test_saved_draft_blocks_scope_and_material_conflicts_without_fetch(workflow_env):
    env=workflow_env
    bind(env)
    workflow=ContentWorkflow(env.store, lambda *args: pytest.fail('不能重新取数'))
    state=env.store.content_workflow(env.identifier)
    state=workflow._save(env.identifier,state,weekly_inputs={'staff_and_requirements':'我们只有3张照片，不补拍。'})
    data=_valid_result()
    data['candidate_topics'][0]['evidence'][0]['fact']='全文未出现尺寸图。'
    data['candidate_topics'][0]['planned_photo_count']=6
    from test_content_research import synthetic_detail
    url = data['source_references'][0]['url']
    state = workflow._save(env.identifier, state, research_calls={'synthetic-detail': {'tool': 'social_get_post_detail',
        'params': {'url': url}, 'status': 'success', 'result': synthetic_detail(url)}})
    saves=[]
    def save(**payload):
        saves.append(payload)
        return {'success':False,'message':'测试仅检查是否到达保存器'}
    state,result=workflow._save_research(env.identifier,state,data,save)
    assert not result['success'] and not saves
    assert state['research_draft'] and len(result['validation_errors'])>=2
    data['candidate_topics'][0]['evidence'][0]['fact']='仅正文提到收纳需求，图片范围未核实。'
    for topic in data['candidate_topics']:
        topic['planned_photo_count']=3
        topic['required_materials']=['3张现有照片，未收到前待核实。']
    state,result=workflow._save_research(env.identifier,state,data,save)
    assert len(saves)==1


def test_topic_detail_displays_read_scope_and_required_count():
    from web_agent.business_text import topic_lines
    topic=_valid_result()['candidate_topics'][0]
    topic.update(visual_refs=['v1'],planned_photo_count=3)
    receipt={'analysis_id':'v1','available_image_count':6,'image_indexes':[1],'analysis':ANALYSIS}
    text='\n'.join(topic_lines(topic,1,[receipt]))
    assert '已读图号：1；详情返回6张图片' in text
    assert '现有照片 3 张' in text
