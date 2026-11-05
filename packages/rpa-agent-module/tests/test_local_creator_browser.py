"""本地真实浏览器验收入口；默认不启动 Chrome，不接触真实平台或账号。"""

import json
import os
from pathlib import Path

import pytest
from PIL import Image

from rpa_agent_module.database import JobRepository
from rpa_agent_module.config import RpaModuleConfig
from rpa_agent_module.environment import subprocess_environment
from rpa_agent_module.jobs import JobManager
from rpa_agent_module.models import verified_draft_result
from rpa_agent_module.workflows.xhs_keywords import XhsWorkflowLibrary


@pytest.mark.skipif(os.environ.get("RUN_RPA_LOCAL_BROWSER") != "1", reason="需显式启用临时Profile本地浏览器验收")
@pytest.mark.parametrize("mode", ["normal", "missing_body", "missing_tags", "missing_image", "same_title", "save_timeout", "selector_changed"])
def test_local_creator_persistence_readback(tmp_path, mode):
    """对实际DOM上传、填写、刷新并读回，异常页不能产生完整成功回执。"""

    from playwright.sync_api import sync_playwright
    fixture = tmp_path / "creator.html"
    fixture.write_text((Path(__file__).parent / "fixtures/xiaohongshu_creator.html").read_text("utf-8"), "utf-8")
    image = tmp_path / "case.png"
    Image.new("RGB", (8, 8), "white").save(image)
    repo = JobRepository(tmp_path / "jobs.db")
    repo.initialize()
    post = {"title": "合成标题", "body": "合成正文", "tags": ["装修"], "image_paths": [str(image)]}
    job = repo.create("synthetic-job", "xiaohongshu_draft", post)
    repo.update_running(job.id)
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps({"job_id": job.id, "database_path": str(repo.database_path),
        "post": post, "image_paths": post["image_paths"]}), "utf-8")
    result_file = tmp_path / "result.json"
    library = XhsWorkflowLibrary(str(input_file), str(result_file))
    with sync_playwright() as playwright:
        executable = os.environ.get("RPA_TEST_CHROME_PATH") or playwright.chromium.executable_path
        assert Path(executable).is_file(), "请提供已安装Chrome路径；此测试不下载浏览器"
        browser = playwright.chromium.launch(executable_path=executable, headless=True,
            env=subprocess_environment(), args=["--disable-background-networking", "--disable-component-update", "--disable-sync"])
        context = browser.new_context()
        requests = []
        context.on("request", lambda request: requests.append(request.url))
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(("file:", "data:")) else route.abort())
        page = context.new_page()
        try:
            page.goto(fixture.as_uri() + "?mode=" + mode)
            if page.locator(".tiptap").count() == 1:
                page.locator(".upload-input").set_input_files(str(image))
                page.locator("input[placeholder]").fill(post["title"])
                page.locator(".tiptap").fill(library.get_post_body())
                page.locator(".tiptap").press("Tab")
                before_reload = page.locator(".tiptap").evaluate("element => ({html:element.innerHTML,text:element.textContent,visible_text:element.innerText,saved:localStorage.getItem('synthetic-draft')})")
                page.reload()
                page.locator(".header-draft").click()
                titles = page.locator(".draft-title-text")
                if titles.count() == 1:
                    titles.click()
                    after_reopen = page.locator(".tiptap").evaluate("element => ({html:element.innerHTML,text:element.textContent,visible_text:element.innerText})")
                    (tmp_path / "dom-receipt.json").write_text(json.dumps({"before_reload": before_reload,
                        "after_reopen": after_reopen}, ensure_ascii=False, indent=2), "utf-8")
                    page.locator(".tiptap").screenshot(path=str(tmp_path / "draft-body.png"))
                    library.mark_draft_saved(page.url, page.locator("input[placeholder]").input_value(),
                        page.locator(".tiptap").inner_text(), page.locator("img.preview").count())
                else:
                    assert titles.count() == (2 if mode == "same_title" else 0)
                    assert mode in {"same_title", "save_timeout"}
            else:
                assert mode == "selector_changed"
            if result_file.is_file():
                result = json.loads(result_file.read_text("utf-8"))
                assert verified_draft_result(result, post) is (mode == "normal")
            else:
                assert mode in {"same_title", "save_timeout", "selector_changed"}
            manager = JobManager(RpaModuleConfig(data_dir=tmp_path, csv_output_dir=tmp_path / "csv"), repo)
            manager._finalize_robot_result(job.id, 0 if result_file.is_file() else 1, result_file, "xiaohongshu_draft.robot")
            status = repo.get(job.id).status
            assert status == ("succeeded" if mode == "normal" else "needs_review")
            (tmp_path / "browser-observation.json").write_text(json.dumps({"mode": mode,
                "requests": requests, "job_status": status, "final_publish_performed": False},
                ensure_ascii=False, indent=2), "utf-8")
        finally:
            browser.close()
