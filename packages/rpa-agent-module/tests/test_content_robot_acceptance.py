"""合成上游与回环页面下的完整 Web→Robot→WebDriver 验收，不访问真实账号。"""

from dataclasses import replace
from functools import partial
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import hashlib
import threading
import time
from types import SimpleNamespace
from uuid import uuid4
import xml.etree.ElementTree as ET

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from PIL import Image
import uvicorn

from rpa_agent_module import RpaExecutionModule
from rpa_agent_module.database import JobRepository
from rpa_agent_module.models import verified_draft_result
from tools import rpa_xiaohongshu
from tools.content_drafts import SAVE_DRAFT_TOOL
from web_agent import app as web_app
from web_agent.content_workflow import ContentWorkflow
from web_agent.conversations import SQLiteConversationStore


RUN_BROWSER = pytest.mark.skipif(
    os.environ.get("RUN_RPA_ROBOT_ACCEPTANCE") != "1",
    reason="需先通过本地 ChromeDriver 预检；使用 scripts/accept_content_robot.py 启动",
)


@pytest.fixture
def acceptance_env(tmp_path, monkeypatch, request):
    """仅替换模型、已保存研究和固定部署配置；执行器、查询及状态转换均用生产实现。"""

    candidate_root = Path(__file__).resolve().parents[3]
    assert Path(web_app.__file__).is_relative_to(candidate_root)
    assert Path(rpa_xiaohongshu.__file__).is_relative_to(candidate_root)
    assert Path(sys.modules[RpaExecutionModule.__module__].__file__).is_relative_to(candidate_root)
    evidence = os.environ.get("RPA_ACCEPTANCE_EVIDENCE")
    root = (Path(evidence) / request.node.name).resolve() if evidence else tmp_path
    root.mkdir(parents=True, exist_ok=True)
    images, data = root / "images", root / "rpa"
    images.mkdir()
    image = root / "synthetic-input.png"
    Image.new("RGB", (16, 16), "red").save(image)
    monkeypatch.setattr(rpa_xiaohongshu, "RPA_XHS_IMAGE_ROOT", images)
    monkeypatch.setattr(rpa_xiaohongshu, "RPA_XHS_DATA_DIR", data)
    monkeypatch.setattr(rpa_xiaohongshu, "RPA_XHS_WAIT_TIMEOUT_SECONDS", 90)
    store = SQLiteConversationStore(root / "web.sqlite3")
    identifier = store.create()
    state = {"status": "plan_ready", "goal_ref": {"artifact_path": "synthetic-only"},
        "weekly_inputs": {"plan_id": str(uuid4()), "staff_and_requirements": "合成输入照片"},
        "weekly_saved": {"version": 1, "markdown": "合成已保存周计划", "content_ids": {"收纳": "C01"}},
        "research_calls": {}}
    store.save_content_workflow(identifier, state, expected_revision=0)
    tracker = SimpleNamespace(mode="normal", creator_url=None, factories=[], generations=0, requests=[], dialogs=[])
    # 上游目标与研究是明确合成替身，不把本次验收称作真实模型或网络研究验收。
    monkeypatch.setattr(ContentWorkflow, "_goal", lambda self, state: {"company_name": "本地合成企业"})
    monkeypatch.setattr(ContentWorkflow, "_research", lambda self, state: {
        "source_references": [{"source_id": "S01", "url": tracker.creator_url + "/source"}],
        "candidate_topics": [{"title": "收纳"}], "limitations": ["上游模型、目标、计划与研究均为合成替身"]})

    class SyntheticAgent:
        def run(self, task, **kwargs):
            assert set(self.tool_handlers) == {SAVE_DRAFT_TOOL}
            assert self.memory is None
            tracker.generations += 1
            result = self.tool_handlers[SAVE_DRAFT_TOOL](title="合成收纳验收", body="先观察动线，再安排收纳位置。",
                tags=["收纳", "动线"], image_notes="仅为本次合成图片，不核验图片内容。", source_refs=["S01"],
                claims=[{"text": "先观察动线，再安排收纳位置。", "source_refs": ["S01"]}], missing_materials=[])
            assert result["success"], result

    def module_factory(config):
        assert config.data_dir == data and config.allowed_image_roots == (images,)
        with store._connect() as connection:
            intent = connection.execute("SELECT execution_id,job_id,status FROM content_draft_executions").fetchall()
        assert len(intent) == 1 and intent[0][1:] == (None, "running")
        module = RpaExecutionModule(replace(config,
            csv_output_dir=root / "csv",
            xiaohongshu_creator_url=tracker.creator_url + "?mode=" + tracker.mode,
            xiaohongshu_selectors=replace(config.xiaohongshu_selectors, challenge="css:.synthetic-challenge")))
        original_environment = module.manager._build_subprocess_environment
        def isolated_environment():
            # 仅本测试实例增加固定隔离常量，不透传宿主密钥或改变生产环境白名单。
            environment = {**original_environment(), "PYTHONDONTWRITEBYTECODE": "1", "SE_OFFLINE": "true"}
            guard = Path(os.environ["RPA_ACCEPTANCE_GUARD_DIR"])
            assert guard.is_relative_to(Path(os.environ["RPA_ACCEPTANCE_EVIDENCE"]))
            environment["PYTHONPATH"] = str(guard) + os.pathsep + environment["PYTHONPATH"]
            return environment
        module.manager._build_subprocess_environment = isolated_environment
        tracker.factories.append(module)
        return module

    monkeypatch.setattr(web_app, "save_xiaohongshu_draft", partial(
        rpa_xiaohongshu.save_xiaohongshu_draft, module_factory=module_factory))
    # query_xiaohongshu_draft 仍是原函数，直接读取与模块相同的临时 jobs.db。
    assert web_app.query_xiaohongshu_draft is rpa_xiaohongshu.query_xiaohongshu_draft
    app = web_app.create_app(lambda *args: SyntheticAgent(), conversation_store=store)
    fixture = (Path(__file__).parent / "fixtures/xiaohongshu_creator.html").read_text("utf-8")
    auth = (Path(__file__).parent / "fixtures/creator_auth_modes.js").read_text("utf-8")

    @app.get("/__acceptance__/creator", response_class=HTMLResponse)
    def creator():
        return HTMLResponse(fixture + "<script>" + auth + "</script>", headers={
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'"})

    client = TestClient(app, raise_server_exceptions=False)
    env = SimpleNamespace(root=root, images=images, data=data, image=image, store=store, identifier=identifier,
        tracker=tracker, app=app, client=client, path=f"/api/conversations/{identifier}/content-production")
    yield env
    # 仅结束此次工厂真实创建且仍在运行的临时 Profile 进程；不枚举或干预其他 Chrome。
    for module in tracker.factories:
        module.shutdown()
        session_file = module.manager.xiaohongshu_browser.session_file
        if session_file.is_file():
            session = json.loads(session_file.read_text("utf-8"))
            assert Path(session["profile_dir"]).is_relative_to(data)
            try:
                os.kill(session["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
    client.close()


def seed_candidate(env, origin="http://127.0.0.1:1"):
    """通过真实候选保存接口建立待审核版本；来源只指向回环地址。"""

    env.tracker.creator_url = origin + "/__acceptance__/creator"
    state = env.store.content_workflow(env.identifier)
    source = env.tracker.creator_url + "/source"
    state["research_calls"] = {"synthetic01": {"tool": "social_get_post_detail", "params": {"url": source},
        "status": "success", "result": {"success": True, "items": [{"url": source,
            "content": "先观察动线，再安排收纳位置。"}], "collected_at": "2026-11-06T00:00:00+08:00"}}}
    env.store.save_content_workflow(env.identifier, state, expected_revision=state["revision"])
    response = env.client.post(env.path, json={"plan_version": 1, "content_id": "C01", "idempotency_key": str(uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


def event(env, draft, action, **arguments):
    artifact = env.store.generation_ui_artifact(env.identifier, draft["artifact_id"])
    return {"conversation_id": env.identifier, "message_id": artifact.message_id, "artifact_id": artifact.artifact_id,
        "scenario": "content_draft", "event_type": "confirm", "action_id": action,
        "arguments": {"result_ref": draft["result_ref"], "version": draft["version"], **arguments},
        "idempotency_key": str(uuid4())}


def api_action(env, draft, action, **arguments):
    return env.client.post("/api/generation/event", json=event(env, draft, action, **arguments))


def latest(env):
    response = env.client.get(env.path)
    assert response.status_code == 200, response.text
    return response.json()["drafts"][0]


def counts(env):
    with env.store._connect() as connection:
        intents = connection.execute("SELECT COUNT(*) FROM content_draft_executions").fetchone()[0]
    jobs = 0
    if (env.data / "jobs.db").is_file():
        with sqlite3.connect(env.data / "jobs.db") as connection:
            jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    return intents, jobs


@pytest.mark.parametrize("rejection", ["unapproved", "stale_version", "changed_image"])
def test_local_acceptance_rejects_before_any_job(acceptance_env, rejection):
    """离线校验三个审批入口拒绝都没有执行意图、任务或浏览器。"""

    env = acceptance_env
    draft = seed_candidate(env)
    uploaded = env.client.post("/api/content-assets/images?filename=synthetic.png", content=env.image.read_bytes(),
        headers={"Content-Type": "image/png"})
    assert uploaded.status_code == 201, uploaded.text
    image_path = uploaded.json()["path"]
    assert Path(image_path).is_relative_to(env.images)
    assert api_action(env, draft, "edit_content_draft", title=draft["title"], body=draft["body"],
        tags_text="收纳\n动线", image_paths_text=image_path).status_code == 200
    draft = latest(env)
    if rejection != "unapproved":
        assert api_action(env, draft, "review_content_draft", decision="approve").status_code == 200
    if rejection == "stale_version":
        assert api_action(env, draft, "edit_content_draft", title="合成新标题", body=draft["body"],
            tags_text="收纳\n动线", image_paths_text=image_path).status_code == 200
    if rejection == "changed_image":
        Image.new("RGB", (16, 16), "blue").save(image_path)
    rejected = api_action(env, draft, "save_content_draft")
    assert rejected.status_code == 409, rejected.text
    expected = {"unapproved": "只有已审核", "stale_version": "当前版本", "changed_image": "图片内容已变化"}
    assert expected[rejection] in rejected.text
    assert counts(env) == (0, 0) and not env.tracker.factories
    (env.root / "rejection.json").write_text(json.dumps({"case": rejection,
        "http_status": rejected.status_code, "response": rejected.json(), "execution_intents": 0, "jobs": 0},
        ensure_ascii=False, indent=2), "utf-8")


@pytest.fixture
def live_ui(acceptance_env, request):
    """提供真实 FastAPI 静态 UI 与回环模拟后台，浏览器控制只负责点击用户界面。"""

    from playwright.sync_api import Error as PlaywrightError, sync_playwright
    env = acceptance_env
    # 缺驱动时在任何浏览器启动前停止；正式运行仍须先确认禁止下载措施。
    assert shutil.which("chromedriver"), "没有PATH中的本地驱动；禁止启动并触发下载回退"
    assert getattr(sys.modules.get("sitecustomize"), "RPA_ACCEPTANCE_NO_DOWNLOAD", False), "必须通过验收启动器安装禁止下载措施"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(env.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    def stop_server():
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
    request.addfinalizer(stop_server)
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.05)
    assert server.started, "本地 FastAPI 未启动；不继续创建 RPA 任务"
    origin = f"http://127.0.0.1:{port}"
    seed_candidate(env, origin)
    with sync_playwright() as playwright:
        chrome = os.environ.get("RPA_TEST_CHROME_PATH", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        browser = playwright.chromium.launch(executable_path=chrome, headless=True,
            args=["--disable-background-networking", "--disable-component-update", "--disable-sync"])
        video_root = os.environ.get("RPA_ACCEPTANCE_VIDEO_ROOT")
        video_options = {"record_video_dir": str(Path(video_root) / request.node.name),
            "record_video_size": {"width": 1280, "height": 720}} if video_root else {}
        context = browser.new_context(**video_options)
        blocked = []
        def local_requests(route):
            url = route.request.url
            if url.startswith(origin + "/") or url.startswith("data:"):
                route.continue_()
            else:
                blocked.append(url)
                route.abort()
        context.route("**/*", local_requests)
        context.add_init_script("localStorage.setItem('testagent.conversation_id', " + json.dumps(env.identifier) + ")")
        page = context.new_page()
        console = []
        page.on("console", lambda message: console.append({"type": message.type, "text": message.text}))
        page.on("pageerror", lambda error: console.append({"type": "pageerror", "text": str(error)}))
        page.on("request", lambda req: env.tracker.requests.append({"method": req.method, "url": req.url}))
        def accept_confirmation(dialog):
            # 合成操作员确认真实UI弹窗，仍由原审核接口保存版本与批准，不直接写业务状态。
            assert dialog.type == "confirm"
            env.tracker.dialogs.append(dialog.message)
            dialog.accept()
        page.on("dialog", accept_confirmation)
        try:
            page.goto(origin)
            card = page.locator(".op-draft").last
            card.get_by_label("选择本机图片", exact=True).set_input_files(str(env.image))
            card.get_by_text("图片已上传；请保存为新版本后审核。", exact=True).wait_for()
            with page.expect_response(lambda response: response.url.endswith("/api/generation/event") and response.request.method == "POST") as edited:
                card.get_by_role("button", name="保存修改和图片为新版本", exact=True).click()
            assert edited.value.status == 200, edited.value.text()
            page.reload()
            card = page.locator(".op-draft").last
            assert "第 2 版" in card.inner_text()
            with page.expect_response(lambda response: response.url.endswith("/api/generation/event") and response.request.method == "POST") as approval:
                card.get_by_role("button", name="批准已保存的此版本", exact=True).click()
            assert approval.value.status == 200, approval.value.text()
            assert counts(env) == (0, 0)
            env.page, env.card, env.blocked = page, card, blocked
            yield env
            assert not blocked, blocked
        except Exception as error:
            # 仅保存本次合成页面的失败证据；初始化失败也必须关闭本次Chrome。
            diagnosis = {"error": str(error), "console": console, "blocked": blocked,
                "requests": env.tracker.requests, "confirmed_dialogs": env.tracker.dialogs}
            try:
                (env.root / "ui-setup-failure.html").write_text(page.content(), "utf-8")
                page.screenshot(path=str(env.root / "ui-setup-failure.png"), full_page=True)
            except PlaywrightError as capture_error:
                diagnosis["capture_error"] = str(capture_error)
            (env.root / "ui-setup-failure.json").write_text(json.dumps(diagnosis, ensure_ascii=False, indent=2), "utf-8")
            raise
        finally:
            try:
                if video_root:
                    # context关闭后视频才完整落盘；只录合成UI，Robot过程另由原Job/XML回执证明。
                    context.close()
                    video = Path(page.video.path())
                    receipt = env.root / "acceptance.json"
                    (env.root / "video-manifest.json").write_text(json.dumps({
                        "model": "synthetic", "source": "synthetic", "platform": "loopback",
                        "captured_surface": "本地合成FastAPI UI页面，不含桌面、其他程序、个人Profile或RPA Chrome窗口",
                        "video": str(video), "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                        "bytes": video.stat().st_size, "case_outcome": "以pytest.xml为准，视频文件存在不代表成功",
                        "acceptance_receipt": str(receipt) if receipt.is_file() else None,
                        "confirmed_dialogs": env.tracker.dialogs}, ensure_ascii=False, indent=2), "utf-8")
            finally:
                browser.close()


def save_from_ui(env):
    with env.page.expect_response(lambda response: response.url.endswith("/api/generation/event")
        and response.request.method == "POST", timeout=110000) as response:
        env.card.get_by_role("button", name="确认启动 RPA 保存草稿", exact=True).click()
    return response.value


def assert_receipt(env, job_status, business_status, *, robot_result=True):
    draft = latest(env)
    with env.store._connect() as connection:
        row = connection.execute("SELECT execution_id,job_id,status,result_json FROM content_draft_executions").fetchone()
    execution, job_id, status, result_json = row
    job = JobRepository(env.data / "jobs.db").get_by_execution_id(execution)
    assert job.id == job_id and draft["execution_id"] == execution
    assert job.status == job_status and status == business_status and draft["execution_status"] == business_status
    assert counts(env) == (1, 1) and len(env.tracker.factories) == 1 and env.tracker.generations == 1
    query = rpa_xiaohongshu.query_xiaohongshu_draft(execution_id=execution, job_id=job_id)
    assert query["job_id"] == job.id and query["success"] is (job_status == "succeeded")
    job_dir = env.data / "jobs" / job.id
    payload = json.loads((job_dir / "input.json").read_text("utf-8"))
    assert payload["database_path"] == str(env.data / "jobs.db")
    assert payload["target_url"].startswith(env.tracker.creator_url + "?mode=")
    assert payload["post"] == {key: draft[key] for key in ("title", "body", "tags")}
    assert all(Path(path).is_relative_to(env.images) for path in payload["image_paths"])
    xml = ET.parse(job_dir / "output.xml").getroot()
    assert xml.attrib["generator"].startswith("Robot ")
    names = [keyword.attrib.get("name") for keyword in xml.iter("kw")]
    assert "Attach Chrome Browser" in names and "Update Job Step" in names
    if job_status == "needs_user_action":
        # Robot会把未走到的分支节点保存在XML中，必须按执行状态判断是否真正操作了页面。
        writes = [keyword for keyword in xml.iter("kw") if keyword.attrib.get("name") in {"Choose File", "Input Text"}]
        assert writes and all(keyword.find("status").attrib["status"] == "NOT RUN" for keyword in writes)
        assert any(keyword.attrib.get("name") == "Mark Needs User Action"
            and keyword.find("status").attrib["status"] == "PASS" for keyword in xml.iter("kw"))
    if job_status == "succeeded":
        assert "Open Exact Draft" in names and "Mark Draft Saved" in names
        assert verified_draft_result(job.result, job.input)
        assert job.result["verification"]["draft_reopened"] is True
        assert set(job.result["verification"]["fields"]) == {"title", "body", "image_count", "tags"}
    if robot_result:
        assert (job_dir / "result.json").is_file()
    env.page.reload()
    env.page.locator(".op-draft").last.get_by_text({"draft_saved": "草稿已核验保存", "needs_review": "待人工核验"}[business_status], exact=True).wait_for()
    assert counts(env) == (1, 1)
    env.page.locator(".op-draft").last.scroll_into_view_if_needed()
    env.page.screenshot(path=str(env.root / "web-after-refresh.png"), full_page=True)
    (env.root / "acceptance.json").write_text(json.dumps({"mode": env.tracker.mode, "execution_id": execution,
        "job_id": job.id, "job_status": job.status, "business_status": status,
        "business_result": json.loads(result_json), "query_result": query, "job_input": payload,
        "ui_requests": env.tracker.requests, "confirmed_dialogs": env.tracker.dialogs,
        "source_boundary": "合成上游、回环页面、临时Profile，无真实账号",
        "source_paths": {"web": web_app.__file__, "adapter": rpa_xiaohongshu.__file__,
            "rpa_module": sys.modules[RpaExecutionModule.__module__].__file__},
        "verification_boundary": "仅核验标题、正文、标签文字、图片数量；不核验图片内容或平台原生话题",
        "final_publish_performed": False}, ensure_ascii=False, indent=2), "utf-8")
    return job


@RUN_BROWSER
@pytest.mark.parametrize("iteration", [1, 2, 3])
def test_real_ui_to_robot_success_three_times(live_ui, iteration):
    env = live_ui
    response = save_from_ui(env)
    assert response.status == 200, response.text()
    assert_receipt(env, "succeeded", "draft_saved")


@RUN_BROWSER
@pytest.mark.parametrize("mode", ["missing_body", "missing_tags", "missing_image", "same_title", "save_timeout", "no_login", "challenge"])
def test_real_robot_failure_states_keep_original_job(live_ui, mode):
    env = live_ui
    env.tracker.mode = mode
    response = save_from_ui(env)
    assert response.status == 200, response.text()
    job = assert_receipt(env, "needs_user_action" if mode in {"no_login", "challenge"} else "needs_review",
        "needs_review", robot_result=mode not in {"same_title", "save_timeout"})
    if mode in {"no_login", "challenge"}:
        assert job.current_step == "needs_user_action"
    elif mode in {"missing_body", "missing_tags", "missing_image"}:
        assert not verified_draft_result(job.result, job.input)


@RUN_BROWSER
def test_real_robot_success_then_failed_business_write_recovers_without_replay(live_ui):
    env = live_ui
    # 在真实 SQLite 写回边界制造一次失败，保留已经成功的原始 RPA Job，不伪造执行结果。
    with env.store._connect() as connection:
        connection.execute("CREATE TRIGGER acceptance_write_failure BEFORE UPDATE OF data_json ON content_draft_versions "
            "WHEN json_extract(NEW.data_json,'$.execution_status')='draft_saved' "
            "BEGIN SELECT RAISE(ABORT,'synthetic business write failure'); END")
    response = save_from_ui(env)
    assert response.status == 500
    with env.store._connect() as connection:
        execution, job_id, status = connection.execute("SELECT execution_id,job_id,status FROM content_draft_executions").fetchone()
        assert status == "running"
        connection.execute("DROP TRIGGER acceptance_write_failure")
    job = JobRepository(env.data / "jobs.db").get_by_execution_id(execution)
    assert job.id == job_id and job.status == "succeeded" and verified_draft_result(job.result, job.input)
    assert counts(env) == (1, 1)
    env.page.reload()
    assert_receipt(env, "succeeded", "draft_saved")


def test_download_guard_blocks_both_download_entries_and_preserves_explicit_driver_resolution(tmp_path):
    """仅验证真实库的本地路径解析，不启动伪驱动，也不把此项称为浏览器执行证据。"""

    guard = tmp_path / "guard"
    guard.mkdir()
    source = (Path(__file__).parent / "fixtures/no_download_sitecustomize.py").read_text("utf-8")
    (guard / "sitecustomize.py").write_text(source, "utf-8")
    script = """
import json,sys
from RPA.core import webdriver as rpa_webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.driver_finder import DriverFinder
from selenium.webdriver.common.selenium_manager import SeleniumManager
blocked=[]
for name,call in [('rpa',lambda:rpa_webdriver.download('Chrome')),('selenium',lambda:SeleniumManager().binary_paths([]))]:
    try:
        call()
    except RuntimeError as error:
        assert '禁止下载驱动' in str(error)
        blocked.append(name)
    else:
        raise AssertionError('下载入口未阻断')
# 使用真实存在的解释器文件仅校验路径解析；不启动服务，不冒充已安装的ChromeDriver。
finder=DriverFinder(Service(executable_path=sys.executable,port=9515),Options())
assert finder.get_driver_path()==sys.executable
print(json.dumps({'blocked':blocked,'explicit_path_resolution':True,'browser_started':False}))
"""
    result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True, env={
        "PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": str(guard),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "ROBOCORP_HOME": str(tmp_path / "robocorp")})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"blocked": ["rpa", "selenium"], "explicit_path_resolution": True, "browser_started": False}
    if os.environ.get("RPA_ACCEPTANCE_EVIDENCE"):
        (Path(os.environ["RPA_ACCEPTANCE_EVIDENCE"]) / "download-guard.json").write_text(result.stdout, "utf-8")
