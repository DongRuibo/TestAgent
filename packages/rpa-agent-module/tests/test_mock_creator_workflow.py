"""执行真实Robot流程和核验关键字，浏览器由离线合成后台替身提供。"""

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image
from robot import run
from robot.libraries.BuiltIn import BuiltIn

from rpa_agent_module.config import RpaModuleConfig, XiaohongshuSelectors
from rpa_agent_module.database import JobRepository
from rpa_agent_module.jobs import JobManager


class Element:
    def __init__(self, kind, text=""):
        self.kind, self.text = kind, text
    def is_displayed(self): return True


class CreatorBrowser:
    """只实现Robot所需页面契约，保存值与刷新后的读值分离。"""

    ROBOT_LIBRARY_SCOPE = "SUITE"
    scenario = "normal"
    history = []

    def __init__(self, auto_close=False, run_on_failure="Capture Page Screenshot"):
        assert run_on_failure is None, "Robot必须关闭默认整页失败截图"
        self.title, self.body, self.images = "", "", 0
        self.saved = None

    def attach_chrome_browser(self, port): self.history.append("attach")
    def switch_window(self, target): pass
    def go_to(self, url): self.history.append("open_local_mock")
    def maximize_browser_window(self): pass
    def wait_until_page_contains_element(self, selector, timeout):
        if "preview" in selector and not self.images:
            raise AssertionError("合成上传失败")
    def wait_until_element_is_visible(self, selector, timeout): pass
    def wait_until_element_contains(self, selector, text, timeout):
        if self.saved is None:
            raise AssertionError("合成保存超时")
    def choose_file(self, selector, value):
        self.images = len(value.splitlines())
        self.history.append("upload")
    def input_text(self, selector, value):
        if "placeholder" in selector:
            self.title = value
        else:
            if self.scenario == "selector_changed":
                raise AssertionError("合成编辑器定位器变化")
            self.body = value
        self.history.append("fill")
    def press_keys(self, selector, key):
        if self.scenario != "save_timeout":
            self.saved = {"title": self.title, "body": self.body, "images": self.images}
            if self.scenario == "missing_body": self.saved["body"] = ""
            if self.scenario == "missing_tags": self.saved["body"] = self.body.split("\n\n#")[0]
            if self.scenario == "missing_image": self.saved["images"] = 0
        self.history.append("save")
    def reload_page(self):
        self.title, self.body, self.images = "", "", 0
        self.history.append("reload")
    def click_element(self, element):
        if isinstance(element, Element) and element.kind == "draft":
            self.title, self.body, self.images = self.saved["title"], self.saved["body"], self.saved["images"]
            self.history.append("reopen")
    def get_webelements(self, selector):
        if "tab-item" in selector: return [Element("tab")]
        amount = 2 if self.scenario == "same_title" else 1
        return [Element("draft", self.saved["title"]) for _ in range(amount)]
    def element_should_be_visible(self, element): pass
    def element_should_be_enabled(self, element): pass
    def get_element_attribute(self, selector, attribute): return self.title
    def get_text(self, selector): return self.body
    def get_element_count(self, selector): return self.images if "preview" in selector else 1
    def capture_element_screenshot(self, selector, path): Image.new("RGB", (8, 8), "white").save(path)
    def get_location(self): return "http://127.0.0.1/synthetic-creator"


@pytest.mark.parametrize("scenario", ["normal", "missing_body", "missing_tags", "missing_image", "same_title", "save_timeout", "selector_changed"])
def test_real_robot_flow_with_offline_creator(tmp_path, monkeypatch, scenario):
    """关键字与Robot实际运行；这不替代真实Chrome/平台端到端验收。"""

    for name in ("RPA", "RPA.Browser"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    selenium = ModuleType("RPA.Browser.Selenium")
    selenium.Selenium = CreatorBrowser
    monkeypatch.setitem(sys.modules, "RPA.Browser.Selenium", selenium)
    sys.modules["RPA"].Browser = sys.modules["RPA.Browser"]
    sys.modules["RPA.Browser"].Selenium = selenium
    monkeypatch.setattr(BuiltIn, "sleep", lambda self, time_, reason=None: None)
    CreatorBrowser.scenario, CreatorBrowser.history = scenario, []
    repo = JobRepository(tmp_path / "jobs.db")
    repo.initialize()
    image = tmp_path / "case.png"
    Image.new("RGB", (8, 8), "white").save(image)
    post = {"title": "合成标题", "body": "合成正文", "tags": ["装修"], "image_paths": [str(image)]}
    repo.create("mock-job", "xiaohongshu_draft", post)
    repo.update_running("mock-job")
    input_file, result_file = tmp_path / "input.json", tmp_path / "result.json"
    input_file.write_text(json.dumps({"job_id": "mock-job", "database_path": str(repo.database_path),
        "target_url": "http://127.0.0.1/synthetic-creator", "debug_port": 9222, "debug_target_id": "page-1",
        "profile_dir": str(tmp_path / "profile"), "selectors": XiaohongshuSelectors().as_dict(),
        "post": post, "image_paths": post["image_paths"]}), "utf-8")
    workflow = Path(__file__).parents[1] / "src/rpa_agent_module/workflows/xiaohongshu_draft.robot"
    code = run(str(workflow), variable=[f"INPUT_FILE:{input_file}", f"RESULT_FILE:{result_file}"],
               outputdir=str(tmp_path), console="quiet", log=None, report=None, output="output.xml")
    manager = JobManager(RpaModuleConfig(data_dir=tmp_path, csv_output_dir=tmp_path / "csv"), repo)
    manager._finalize_robot_result("mock-job", code, result_file, "xiaohongshu_draft.robot")
    assert repo.get("mock-job").status == ("succeeded" if scenario == "normal" else "needs_review"), (code, repo.get("mock-job"), CreatorBrowser.history)
    if scenario == "normal":
        assert CreatorBrowser.history.index("save") < CreatorBrowser.history.index("reload") < CreatorBrowser.history.index("reopen")
        assert repo.get("mock-job").result["final_publish_performed"] is False
    assert "publish" not in CreatorBrowser.history
