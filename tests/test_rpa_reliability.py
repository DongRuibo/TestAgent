"""草稿关联、故障恢复和完整读回的离线回归，只使用临时合成数据。"""

import fcntl
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from rpa_agent_module.browser_session import ManagedBrowserSession, XiaohongshuBrowserSessionManager
from rpa_agent_module.config import RpaModuleConfig
from rpa_agent_module.database import JobRepository
from rpa_agent_module.jobs import JobManager
from rpa_agent_module.models import XiaohongshuDraftRequest, verified_draft_result
from rpa_agent_module.workflows.xhs_keywords import XhsWorkflowLibrary
from tools import rpa_xiaohongshu as adapter


@pytest.fixture
def draft_env(tmp_path, monkeypatch):
    images = tmp_path / "images"
    images.mkdir()
    image = images / "case.png"
    Image.new("RGB", (8, 8), "white").save(image)
    config = RpaModuleConfig(data_dir=tmp_path / "data", allowed_image_roots=(images,),
                             csv_output_dir=tmp_path / "csv", xiaohongshu_enabled=True)
    repo = JobRepository(config.data_dir / "jobs.db")
    repo.initialize()
    events = []
    browser = SimpleNamespace(ensure=lambda: (events.append("browser") or ManagedBrowserSession(1, 9222, tmp_path / "profile", "page-1")))
    manager = JobManager(config, repo, browser_session=browser)
    monkeypatch.setattr(manager, "_start_thread", lambda *args: events.append("robot"))
    monkeypatch.setattr(adapter, "RPA_XHS_DATA_DIR", config.data_dir)
    request = XiaohongshuDraftRequest(title="合成标题", body="合成正文", tags=["装修"], image_paths=[image])
    return manager, repo, request, events


def saved_result(manager, job, *, title="合成标题", body="合成正文\n\n#装修", image_count=1):
    folder = manager.config.data_dir / "jobs" / job.id
    lib = XhsWorkflowLibrary(str(folder / "input.json"), str(folder / "result.json"))
    lib.mark_draft_saved("https://example.invalid/draft?token=不应保留", title, body, image_count)
    return json.loads((folder / "result.json").read_text("utf-8"))


def test_execution_link_is_durable_before_callback_and_browser(draft_env):
    manager, repo, request, events = draft_env
    execution = str(uuid4())
    def created(job_id):
        assert repo.get_by_execution_id(execution).id == job_id
        assert events == []
        events.append("callback")
    job = manager.create_xiaohongshu_job(request, execution_id=execution, on_job_created=created)
    assert events == ["callback", "browser", "robot"]
    assert job.execution_id == execution


def test_same_execution_does_not_start_a_second_job(draft_env):
    manager, repo, request, events = draft_env
    execution = str(uuid4())
    first = manager.create_xiaohongshu_job(request, execution_id=execution)
    second = manager.create_xiaohongshu_job(request, execution_id=execution)
    assert first.id == second.id
    assert events == ["browser", "robot"]
    from rpa_agent_module.exceptions import JobConflictError
    with pytest.raises(JobConflictError):
        manager.create_xiaohongshu_job(request.model_copy(update={"body": "另一个正文"}), execution_id=execution)


def test_callback_failure_never_starts_browser_and_link_survives(draft_env):
    manager, repo, request, events = draft_env
    execution = str(uuid4())
    def fail(_job_id):
        raise OSError("合成业务回写失败")
    with pytest.raises(OSError):
        manager.create_xiaohongshu_job(request, execution_id=execution, on_job_created=fail)
    assert events == []
    assert repo.get_by_execution_id(execution).error_code == "job_preparation_failed"


def test_readonly_query_does_not_create_missing_database(tmp_path, monkeypatch):
    target = tmp_path / "not-created"
    monkeypatch.setattr(adapter, "RPA_XHS_DATA_DIR", target)
    assert adapter.query_xiaohongshu_draft(execution_id=str(uuid4()))["needs_review"]
    assert not target.exists()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_hard_exit_query_needs_review_without_module_restart(draft_env, status):
    manager, repo, request, _ = draft_env
    execution = str(uuid4())
    job = manager.create_xiaohongshu_job(request, execution_id=execution)
    if status == "running":
        repo.update_running(job.id)
    before = repo.database_path.read_bytes()
    result = adapter.query_xiaohongshu_draft(execution_id=execution)
    assert result["status"] == "needs_review"
    assert not result["success"]
    assert repo.database_path.read_bytes() == before


def test_query_preserves_reliably_active_module(draft_env):
    manager, repo, request, _ = draft_env
    execution = str(uuid4())
    job = manager.create_xiaohongshu_job(request, execution_id=execution)
    repo.update_running(job.id)
    with (manager.config.data_dir / ".module.lock").open("w+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = adapter.query_xiaohongshu_draft(execution_id=execution)
    assert result["status"] == "running"
    assert not result["needs_review"]


def test_full_readback_success_can_recover_business_write_failure(draft_env):
    manager, repo, request, _ = draft_env
    execution = str(uuid4())
    job = manager.create_xiaohongshu_job(request, execution_id=execution)
    result = saved_result(manager, job)
    assert verified_draft_result(result, job.input)
    assert "token" not in result["url"]
    repo.update_running(job.id)
    manager._finalize_robot_result(job.id, 0, manager.config.data_dir / "jobs" / job.id / "result.json", "xiaohongshu_draft.robot")
    queried = adapter.query_xiaohongshu_draft(execution_id=execution)
    assert queried["success"]
    assert queried["job_id"] == job.id
    assert queried["final_publish_performed"] is False


@pytest.mark.parametrize("changes", [
    {"title": "同标题旧草稿"}, {"body": "旧草稿正文\n\n#装修"},
    {"body": "合成正文"}, {"image_count": 0},
])
def test_readback_mismatch_never_reports_saved(draft_env, changes):
    manager, repo, request, _ = draft_env
    job = manager.create_xiaohongshu_job(request, execution_id=str(uuid4()))
    result = saved_result(manager, job, **changes)
    assert not verified_draft_result(result, job.input)
    repo.update_running(job.id)
    manager._finalize_robot_result(job.id, 1, manager.config.data_dir / "jobs" / job.id / "result.json", "xiaohongshu_draft.robot")
    assert repo.get(job.id).status == "needs_review"


def test_title_only_old_success_is_not_complete_verification(draft_env):
    manager, repo, request, _ = draft_env
    job = manager.create_xiaohongshu_job(request, execution_id=str(uuid4()))
    repo.update_running(job.id)
    repo.complete(job.id, {"status": "succeeded", "image_count": 1})
    assert adapter.query_xiaohongshu_draft(execution_id=job.execution_id)["needs_review"]


def test_restart_retains_original_job_and_requires_review(draft_env):
    manager, repo, request, events = draft_env
    job = manager.create_xiaohongshu_job(request, execution_id=str(uuid4()))
    repo.update_running(job.id)
    assert repo.recover_interrupted_jobs() == 1
    assert repo.get_by_execution_id(job.execution_id).status == "needs_review"
    replay = manager.create_xiaohongshu_job(request, execution_id=job.execution_id)
    assert replay.id == job.id
    assert events == ["browser", "robot"]


def test_legacy_database_is_migrated_without_changing_history(tmp_path):
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,workflow TEXT,status TEXT,current_step TEXT,input_json TEXT,result_json TEXT,error_code TEXT,error_message TEXT,created_at TEXT,started_at TEXT,finished_at TEXT)")
        connection.execute("INSERT INTO jobs(id,workflow,status,current_step,input_json,created_at) VALUES ('old','csv_archive','succeeded','completed','{}','old-time')")
    repo = JobRepository(database)
    assert repo.get_by_execution_id("missing") is None
    repo.initialize()
    assert repo.get("old").execution_id is None
    assert repo.get("old").status == "succeeded"


def test_robot_and_chrome_processes_receive_only_allowlisted_environment(draft_env, tmp_path, monkeypatch):
    manager, repo, request, _ = draft_env
    secrets = {"TAVILY_API_KEY": "synthetic-tavily", "E2B_API_KEY": "synthetic-e2b",
               "CUSTOM_SECRET": "synthetic-other", "PYTHONPATH": "/untrusted/import/path"}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    robot_env = manager._build_subprocess_environment()
    assert not set(secrets.values()) & set(robot_env.values())
    assert "untrusted" not in robot_env["PYTHONPATH"]
    chrome = tmp_path / "chrome"
    chrome.write_text("synthetic", encoding="utf-8")
    browser = XiaohongshuBrowserSessionManager(tmp_path / "browser", chrome_path=chrome)
    captured = {}
    def popen(command, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr("rpa_agent_module.browser_session.subprocess.Popen", popen)
    monkeypatch.setattr(browser, "_allocate_loopback_port", lambda: 9222)
    monkeypatch.setattr(browser, "_probe_debug_endpoint", lambda port: True)
    monkeypatch.setattr(browser, "_ensure_managed_page", lambda session: session)
    browser._launch()
    assert not set(secrets) & set(captured["env"])


def test_adapter_callback_runs_before_wait_and_strict_result_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "RPA_XHS_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(adapter, "RPA_XHS_IMAGE_ROOT", tmp_path / "images")
    events = []
    class Module:
        def __init__(self, config): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def health(self): return SimpleNamespace(platform_supported=True, chrome_available=True, database_ready=True)
        def submit(self, workflow, payload, **kwargs):
            self.payload = payload
            kwargs["on_job_created"]("job-1")
            return SimpleNamespace(id="job-1")
        def wait(self, job_id, timeout):
            assert events == ["created"]
            return SimpleNamespace(id=job_id, status="succeeded", error_code=None, error_message=None,
                                   result={"status": "succeeded"}, input=self.payload)
    result = adapter.save_xiaohongshu_draft(title="标题", body="正文", tags=[], image_paths=["/synthetic/image.png"],
        execution_id=str(uuid4()), on_job_created=lambda job_id: events.append("created"), module_factory=Module)
    assert not result["success"]
    assert result["needs_review"]


def test_save_and_query_use_the_same_persisted_normalized_snapshot(draft_env, monkeypatch):
    """真实模块入口规范化一次，保存和恢复均核对任务存档而非原始参数。"""

    from rpa_agent_module.module import RpaExecutionModule
    manager, repo, request, _ = draft_env
    monkeypatch.setattr(adapter, "RPA_XHS_IMAGE_ROOT", manager.config.allowed_image_roots[0])
    module = object.__new__(RpaExecutionModule)
    module._closed, module.manager = False, manager
    monkeypatch.setattr(module, "_chrome_available", lambda: True)
    class LocalModule:
        def __init__(self, config): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def health(self): return SimpleNamespace(platform_supported=True, chrome_available=True, database_ready=True)
        def submit(self, *args, **kwargs): return module.submit(*args, **kwargs)
        def wait(self, job_id, timeout):
            job = repo.get(job_id)
            assert job.input["tags"] == ["装修"]
            saved_result(manager, job)
            repo.update_running(job.id)
            manager._finalize_robot_result(job.id, 0, manager.config.data_dir / "jobs" / job.id / "result.json", "xiaohongshu_draft.robot")
            return repo.get(job.id)
    execution = str(uuid4())
    saved = adapter.save_xiaohongshu_draft(title=" 合成标题 ", body=" 合成正文 ",
        tags=[" #装修 ", "装修"], image_paths=[str(path) for path in request.image_paths],
        execution_id=execution, module_factory=LocalModule)
    queried = adapter.query_xiaohongshu_draft(execution_id=execution)
    assert saved["success"] is True
    assert queried == saved
