from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

import rpa_agent_module
from rpa_agent_module.browser_session import ManagedBrowserSession
from rpa_agent_module.config import RpaModuleConfig
from rpa_agent_module.database import JobRepository
from rpa_agent_module.jobs import JobManager
from rpa_agent_module.models import XiaohongshuDraftRequest


class FakeBrowserSessionManager:
    def __init__(self, session: ManagedBrowserSession) -> None:
        self.session = session
        self.ensure_calls = 0

    def ensure(self) -> ManagedBrowserSession:
        self.ensure_calls += 1
        return self.session

    def state(self) -> str:
        return "managed_ready"


def test_job_passes_managed_debug_port_to_robot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    image_root = tmp_path / "images"
    image_root.mkdir()
    image_path = image_root / "case.png"
    Image.new("RGB", (16, 16), "white").save(image_path)
    config = RpaModuleConfig(
        data_dir=data_dir,
        allowed_image_roots=(image_root,),
        csv_output_dir=tmp_path / "csv",
        xiaohongshu_enabled=True,
    )
    repository = JobRepository(data_dir / "jobs.db")
    repository.initialize()
    browser = FakeBrowserSessionManager(
        ManagedBrowserSession(
            1234,
            9222,
            data_dir / "browser_profiles" / "xiaohongshu",
            "page-1",
        )
    )
    manager = JobManager(config, repository, browser_session=browser)
    monkeypatch.setattr(manager, "_start_thread", lambda *_args: None)

    job = manager.create_xiaohongshu_job(
        XiaohongshuDraftRequest(
            title="标题",
            body="正文",
            tags=["装修"],
            image_paths=[image_path],
        )
    )

    payload = json.loads((data_dir / "jobs" / job.id / "input.json").read_text(encoding="utf-8"))
    assert payload["debug_port"] == 9222
    assert payload["debug_target_id"] == "page-1"
    assert payload["profile_dir"] == str(browser.session.profile_dir)
    assert browser.ensure_calls == 1


def test_robot_attaches_existing_chrome_without_closing_it() -> None:
    package_root = Path(rpa_agent_module.__file__).resolve().parent
    robot = (package_root / "workflows" / "xiaohongshu_draft.robot").read_text(encoding="utf-8")

    assert "Attach Chrome Browser    ${debug_port}" in robot
    assert "Switch Window    ${debug_target_id}" in robot
    assert "Open Browser" not in robot
    assert "Close All Browsers" not in robot
    assert "无需关闭窗口" in robot
    assert "最终发布" in robot
