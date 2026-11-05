"""验证官方后台采集的账号边界、真实导出格式、缺失值及 Agent 交付。"""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import json

from openpyxl import Workbook
import pytest

from rpa_agent_module import RpaModuleConfig, XiaohongshuMetricsAccount, XiaohongshuMetricsRequest
from rpa_agent_module.browser_session import ManagedBrowserSession
from rpa_agent_module.browser_session import XiaohongshuBrowserSessionManager
from rpa_agent_module.database import JobRepository
from rpa_agent_module.jobs import JobManager
from rpa_agent_module.xiaohongshu_metrics import parse_metrics_export
from rpa_agent_module.workflows.xhs_metrics_keywords import UserActionRequired, XhsMetricsLibrary
import tools.rpa_xiaohongshu_metrics as tool

TODAY = datetime.now(ZoneInfo("Asia/Shanghai")).date()
ACCOUNT = XiaohongshuMetricsAccount(user_id="a" * 24, red_id="test_account", nickname="测试账号")
QUERY = XiaohongshuMetricsRequest(start_date=TODAY - timedelta(days=7), end_date=TODAY)
HEADERS = ["笔记标题", "首次发布时间", "体裁", "曝光", "观看量", "封面点击率", "点赞", "评论", "收藏", "涨粉", "分享", "人均观看时长", "弹幕"]


def export_fixture(path: Path, *, rows=None, headers=None):
    """生成测试输入，不运行浏览器，不使用真实用户数据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    sheet = wb.active
    sheet.append(["最多导出排序后前1000条笔记"] * 13)
    sheet.append(headers or HEADERS)
    for row in rows if rows is not None else [source_row()]:
        sheet.append(row)
    wb.save(path)
    wb.close()
    return path


def source_row():
    return ["测试笔记", TODAY.strftime("%Y年%m月%d日") + "12时01分02秒", "图文", 1000, 100, .1, 5, 2, 0, 0, 1, 3, 0]


def parse(path, total=1, query=QUERY):
    return parse_metrics_export(path, query=query, account=ACCOUNT, source_total=total)


def test_official_schema_preserves_missing_zero_and_period(tmp_path):
    row = source_row()
    row[6] = None
    data = parse(export_fixture(tmp_path / "export.xlsx", rows=[row]))
    post = data["posts"][0]
    assert post["views"] == 100
    assert post["cover_click_rate"] == .1
    assert post["likes"] is None
    assert post["saves"] == 0
    assert post["spend"] is None and post["content_id"] is None
    assert data["summary"]["totals"]["likes"] is None
    assert data["date_filter_kind"] == "first_published_at"
    assert data["traffic_scope"] == data["metrics_period"] == "unspecified_by_export"


@pytest.mark.parametrize("value", [-1, "NaN", "inf", "1.2", "50%", "=1+2", True])
def test_invalid_counts_fail_instead_of_becoming_zero(tmp_path, value):
    row = source_row()
    row[3] = value
    with pytest.raises(ValueError):
        parse(export_fixture(tmp_path / "export.xlsx", rows=[row]))


def test_rejects_stale_download_and_incomplete_export(tmp_path):
    path = export_fixture(tmp_path / "export.xlsx")
    with pytest.raises(ValueError, match="总条数"):
        parse(path, total=2)
    row = source_row()
    row[1] = (TODAY - timedelta(days=8)).strftime("%Y年%m月%d日") + "12时00分00秒"
    with pytest.raises(ValueError, match="范围外"):
        parse(export_fixture(path, rows=[row]))


def test_duplicate_records_and_missing_headers_are_not_success(tmp_path):
    path = tmp_path / "export.xlsx"
    with pytest.raises(ValueError, match="重复"):
        parse(export_fixture(path, rows=[source_row(), source_row()]), total=2)
    with pytest.raises(ValueError, match="表头"):
        parse(export_fixture(path, headers=["未知字段"] + HEADERS[1:]))


def test_limit_reports_partial_sample_and_scopes_totals(tmp_path):
    other = source_row()
    other[0] = "第二篇"
    query = QUERY.model_copy(update={"max_posts": 1})
    data = parse(export_fixture(tmp_path / "export.xlsx", rows=[source_row(), other]), total=2, query=query)
    assert data["source_total"] == data["exported_count"] == 2
    assert data["collected_count"] == 1 and data["truncated"] is True
    assert data["summary"]["totals"]["impressions"] == 1000


def test_empty_export_is_not_zero_account_performance(tmp_path):
    data = parse(export_fixture(tmp_path / "export.xlsx", rows=[]), total=0)
    assert data["posts"] == []
    assert data["summary"]["totals"]["impressions"] is None


@pytest.mark.parametrize("changes", [
    {"max_posts": 0}, {"max_posts": 51}, {"max_posts": True}, {"max_posts": "5"},
    {"target_url": "https://example.com"}, {"account": "other"},
    {"start_date": TODAY - timedelta(days=31)}, {"end_date": TODAY + timedelta(days=1)},
    {"end_date": TODAY - timedelta(days=10)},
])
def test_request_rejects_unbounded_or_untrusted_controls(changes):
    with pytest.raises(ValueError):
        XiaohongshuMetricsRequest.model_validate({**QUERY.model_dump(), **changes})


def test_job_uses_fixed_binding_and_failed_result_is_not_success(tmp_path, monkeypatch):
    repo = JobRepository(tmp_path / "jobs.db")
    repo.initialize()
    browser = SimpleNamespace(ensure=lambda: ManagedBrowserSession(123, 9222, tmp_path / "profile", "target"))
    manager = JobManager(RpaModuleConfig(data_dir=tmp_path, xiaohongshu_enabled=True,
                         xiaohongshu_metrics_account=ACCOUNT), repo, browser)
    monkeypatch.setattr(manager, "_start_thread", lambda *args: None)
    job = manager.create_xiaohongshu_metrics_job(QUERY)
    payload = json.loads((tmp_path / "jobs" / job.id / "input.json").read_text())
    assert payload["account"] == ACCOUNT.model_dump()
    assert payload["debug_target_id"] == "target"
    result_file = tmp_path / "jobs" / job.id / "result.json"
    result_file.write_text(json.dumps({"status": "failed", "error_code": "invalid_export", "message": "校验失败"}))
    manager._finalize_robot_result(job.id, 0, result_file, "xiaohongshu_metrics.robot")
    assert repo.get(job.id).status == "failed"
    assert repo.get(job.id).error_code == "invalid_export"


def test_identity_is_verified_by_red_id_not_nickname():
    lib = object.__new__(XhsMetricsLibrary)
    lib.account = ACCOUNT
    lib._step = lambda _: None
    lib._wait = lambda fn: fn()
    lib.driver = SimpleNamespace(get=lambda url: None, find_element=lambda *args: SimpleNamespace(text="D. 小红书账号: wrong_account"))
    with pytest.raises(UserActionRequired, match="不一致"):
        lib._verify_account()


def test_red_id_is_required_even_without_profile_link():
    account = XiaohongshuMetricsAccount(red_id="sample_number", nickname="示例")
    assert account.user_id is None
    with pytest.raises(ValueError):
        XiaohongshuMetricsAccount(nickname="同名账号")


@pytest.mark.parametrize("text", ["请开通数据中心", "数据中心未开通", "请完成验证", "扫码登录"])
def test_platform_prerequisites_require_human_action(text):
    lib = object.__new__(XhsMetricsLibrary)
    lib.driver = SimpleNamespace(current_url="https://creator.xiaohongshu.com/statistics/data-analysis",
                                 find_element=lambda *args: SimpleNamespace(text=text))
    with pytest.raises(UserActionRequired):
        lib._guard()


@pytest.mark.parametrize("same_host,same_pid,alive,allowed", [
    (True, True, False, True), (False, True, False, False),
    (True, False, False, False), (True, True, True, False),
])
def test_crash_recovery_only_for_owned_exited_profile(tmp_path, monkeypatch, same_host, same_pid, alive, allowed):
    import socket
    from rpa_agent_module import JobUnavailableError
    manager = XiaohongshuBrowserSessionManager(tmp_path)
    manager.profile_dir.mkdir(parents=True)
    session = ManagedBrowserSession(1234, 9222, manager.profile_dir, "old")
    manager._write_session(session)
    host = socket.gethostname() if same_host else "different-host"
    pid = 1234 if same_pid else 4321
    lock = manager.profile_dir / "SingletonLock"
    lock.symlink_to(f"{host}-{pid}")
    monkeypatch.setattr(manager, "_pid_is_alive", lambda _: alive)
    monkeypatch.setattr(manager, "_probe_debug_endpoint", lambda _: False)
    replacement = ManagedBrowserSession(5678, 9333, manager.profile_dir, "new")
    monkeypatch.setattr(manager, "_launch", lambda: replacement)
    if allowed:
        assert manager.state() == "closed"
        assert manager.ensure() == replacement
    else:
        with pytest.raises(JobUnavailableError): manager.ensure()
    assert lock.is_symlink()  # 应交由 Chrome 处理锁，Python 不删锁。


def test_process_permission_error_is_not_treated_as_exited(tmp_path, monkeypatch):
    def denied(*args): raise PermissionError()
    monkeypatch.setattr("rpa_agent_module.browser_session.os.kill", denied)
    assert XiaohongshuBrowserSessionManager(tmp_path)._pid_is_alive(1234)


def test_controlled_date_input_replaces_old_value_instead_of_appending():
    from selenium.webdriver.common.keys import Keys
    class ControlledInput:
        value = "2026-08-14"
        selected = False
        def get_attribute(self, name): return self.value
        def click(self): pass
        def clear(self): pass  # 模拟组件在 clear 后恢复旧值。
        def send_keys(self, *keys):
            if Keys.COMMAND in keys:
                self.selected = True
            elif Keys.ENTER not in keys:
                self.value = keys[0] if self.selected else self.value + keys[0]
                self.selected = False
    element = ControlledInput()
    lib = object.__new__(XhsMetricsLibrary)
    lib._visible = lambda selector: element
    def wait(predicate): assert predicate()
    lib._wait = wait
    lib._set_date("start", "2026-08-15")
    assert element.value == "2026-08-15"


class FakeRpa:
    completed = None
    timeout = False
    stopped = False
    submitted = False

    def __init__(self, config):
        assert config.xiaohongshu_metrics_account == ACCOUNT

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def health(self): return SimpleNamespace(platform_supported=True, chrome_available=True, database_ready=True)
    def submit(self, workflow, payload):
        assert workflow == "xiaohongshu_metrics"
        type(self).submitted = True
        return SimpleNamespace(id="job-test")
    def wait(self, job_id, timeout):
        if type(self).timeout: raise TimeoutError()
        return type(self).completed
    def get_job(self, job_id): return SimpleNamespace(is_terminal=False)
    def stop(self, job_id): type(self).stopped = True


@pytest.fixture
def configured_tool(tmp_path, monkeypatch):
    data_dir = tmp_path / ".local" / "rpa"
    data_dir.mkdir(parents=True)
    binding = data_dir / "account.json"
    binding.write_text(ACCOUNT.model_dump_json())
    monkeypatch.setattr(tool, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(tool, "RPA_DATA_DIR", data_dir)
    monkeypatch.setattr(tool, "ACCOUNT_FILE", binding)
    monkeypatch.setattr(tool, "OUTPUT_DIR", tmp_path / "workspace" / "xiaohongshu_metrics")
    monkeypatch.setattr(tool, "RpaExecutionModule", FakeRpa)
    FakeRpa.timeout = FakeRpa.stopped = FakeRpa.submitted = False
    raw = export_fixture(data_dir / "jobs" / "job-test" / "incoming" / "official.xlsx")
    data = parse(raw)
    data["raw_export_path"] = str(raw)
    FakeRpa.completed = SimpleNamespace(id="job-test", status="succeeded", result={"result_type": "xiaohongshu_metrics", "data": data}, error_code=None, error_message=None)
    return tmp_path


def call_tool():
    return tool.collect_xiaohongshu_metrics(QUERY.start_date.isoformat(), QUERY.end_date.isoformat())


def test_tool_delivers_real_local_artifacts_and_registers(configured_tool):
    result = call_tool()
    assert result["success"] is True
    data = json.loads((configured_tool / result["path"]).read_text())
    assert data["posts"][0]["spend"] is None
    assert (configured_tool / result["raw_export_path"]).is_file()
    from agent.loop import DEFAULT_TOOL_HANDLERS, DEFAULT_TOOL_BUDGETS, TOOL_DEFINITIONS
    name = tool.RPA_XHS_METRICS_TOOL_NAME
    assert name in DEFAULT_TOOL_HANDLERS and name in TOOL_DEFINITIONS
    assert DEFAULT_TOOL_BUDGETS[name] == 1


def test_timeout_stops_job_and_no_retry(configured_tool):
    FakeRpa.timeout = True
    result = call_tool()
    assert result["error_type"] == "RPA_TIMEOUT"
    assert FakeRpa.stopped and result["retryable"] is False


def test_missing_binding_does_not_start_browser(configured_tool):
    tool.ACCOUNT_FILE.write_text("{}")
    result = call_tool()
    assert result["needs_user_action"] and not FakeRpa.submitted


@pytest.mark.parametrize("broken", ["account", "missing_file", "outside_file"])
def test_success_status_cannot_hide_wrong_or_missing_data(configured_tool, broken):
    data = FakeRpa.completed.result["data"]
    if broken == "account": data["account"] = {"red_id": "wrong"}
    elif broken == "missing_file": Path(data["raw_export_path"]).unlink()
    else: data["raw_export_path"] = str(export_fixture(configured_tool / "outside.xlsx"))
    assert call_tool()["success"] is False


def test_user_action_preserved_without_output(configured_tool):
    FakeRpa.completed.status = "needs_user_action"
    FakeRpa.completed.error_message = "请登录绑定账号"
    result = call_tool()
    assert result["needs_user_action"] is True
    assert result["message"] == "请登录绑定账号"
    assert not tool.OUTPUT_DIR.exists()


def test_pair_dates_and_permissions_fail_before_false_success(configured_tool, monkeypatch):
    assert tool.collect_xiaohongshu_metrics(start_date=TODAY.isoformat())["error_type"] == "RPA_INVALID_INPUT"
    assert not FakeRpa.submitted
    def denied(*args):
        raise PermissionError("private operating system details")
    monkeypatch.setattr(FakeRpa, "submit", denied)
    result = call_tool()
    assert result["error_type"] == "RPA_HOST_PERMISSION_REQUIRED"
    assert "private operating" not in result["message"]


def test_timeout_reports_collection_stage_and_does_not_write_success(tmp_path, monkeypatch):
    from rpa_agent_module.workflows import xhs_metrics_keywords as worker
    repo = JobRepository(tmp_path / "jobs.db")
    repo.initialize()
    repo.create("test", "xiaohongshu_metrics", {})
    repo.update_running("test")
    payload = {"job_id": "test", "database_path": str(repo.database_path),
               "account": ACCOUNT.model_dump(), "query": QUERY.model_dump(mode="json"),
               "debug_port": 9222, "debug_target_id": "target"}
    input_file, result_file = tmp_path / "input.json", tmp_path / "result.json"
    input_file.write_text(json.dumps(payload))
    browser = SimpleNamespace(create_webdriver=lambda name, options: None,
              driver=SimpleNamespace(set_page_load_timeout=lambda n: None,
                                     switch_to=SimpleNamespace(window=lambda handle: None, new_window=lambda kind: None)))
    monkeypatch.setattr(worker, "BuiltIn", lambda: SimpleNamespace(get_library_instance=lambda name: browser))
    lib = XhsMetricsLibrary(str(input_file), str(result_file))
    def timeout():
        lib._step("verifying_metrics_account")
        raise TimeoutError()
    monkeypatch.setattr(lib, "_verify_account", timeout)
    lib.collect_metrics()
    result = json.loads(result_file.read_text())
    assert result["status"] == "needs_user_action"
    assert result["collection_stage"] == "verifying_metrics_account"
    assert "data" not in result
