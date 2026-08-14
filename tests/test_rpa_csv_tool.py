"""验证本机 CSV RPA 工具的受控配置、结果映射和 Agent 注册。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import tools.rpa_csv as rpa_tool
import web_agent.app as web_app
from agent.loop import (
    DEFAULT_TOOL_BUDGETS,
    DEFAULT_TOOL_HANDLERS,
    SYSTEM_PROMPT,
    TOOL_DEFINITIONS,
)


class FakeRpaModule:
    """模拟 wheel 门面，避免单元测试真实启动 Chrome。"""

    health_result = SimpleNamespace(
        platform_supported=True,
        chrome_available=True,
        database_ready=True,
    )
    completed_result: Any = None
    submitted_payload: dict[str, Any] | None = None
    received_config: Any = None

    def __init__(self, config: Any) -> None:
        type(self).received_config = config

    def __enter__(self) -> "FakeRpaModule":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def health(self) -> Any:
        return type(self).health_result

    def submit(self, workflow: str, payload: dict[str, Any]) -> Any:
        assert workflow == "csv_archive"
        type(self).submitted_payload = payload
        return SimpleNamespace(id="job-1")

    def wait(self, job_id: str, timeout: int) -> Any:
        assert job_id == "job-1"
        assert timeout == rpa_tool.RPA_CSV_WAIT_TIMEOUT_SECONDS
        return type(self).completed_result


def configure_fake_paths(monkeypatch, tmp_path) -> None:
    """把工具固定目录重定向到测试临时目录。"""

    FakeRpaModule.health_result = SimpleNamespace(
        platform_supported=True,
        chrome_available=True,
        database_ready=True,
    )
    FakeRpaModule.submitted_payload = None
    FakeRpaModule.received_config = None
    monkeypatch.setattr(rpa_tool, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(rpa_tool, "RPA_DATA_DIR", tmp_path / ".local" / "rpa-agent-module")
    monkeypatch.setattr(rpa_tool, "RPA_CSV_OUTPUT_DIR", tmp_path / "artifacts" / "rpa" / "csv")
    monkeypatch.setattr(rpa_tool, "RpaExecutionModule", FakeRpaModule)


def test_rpa_csv_archive_returns_downloadable_artifact(monkeypatch, tmp_path) -> None:
    """成功任务必须返回受控目录中的真实 CSV 和网页可识别的 Artifact。"""

    configure_fake_paths(monkeypatch, tmp_path)
    output = rpa_tool.RPA_CSV_OUTPUT_DIR
    output.mkdir(parents=True)
    csv_path = output / "业务数据.csv"
    csv_path.write_text("record_id,created_at\nREC-0001,2026-08-01\n", encoding="utf-8")
    FakeRpaModule.completed_result = SimpleNamespace(
        id="job-1",
        status="succeeded",
        result={
            "result_type": "file_archived",
            "has_data": True,
            "query": {"start_date": "2026-08-01", "end_date": "2026-08-12"},
            "final_path": str(csv_path),
        },
        error_code=None,
        error_message=None,
    )

    result = rpa_tool.rpa_csv_archive("2026-08-01", "2026-08-12", keyword="项目")

    assert result["success"] is True
    assert result["artifact_path"] == "artifacts/rpa/csv/业务数据.csv"
    assert result["artifacts"] == [
        {
            "kind": "csv_archive",
            "path": "artifacts/rpa/csv/业务数据.csv",
            "media_type": "text/csv",
            "size_bytes": csv_path.stat().st_size,
        }
    ]
    assert FakeRpaModule.submitted_payload == {
        "start_date": "2026-08-01",
        "end_date": "2026-08-12",
        "region": "全部",
        "status": "全部",
        "keyword": "项目",
        "open_finder": False,
    }
    assert FakeRpaModule.received_config.csv_target_url == rpa_tool.RPA_CSV_TARGET_URL
    assert FakeRpaModule.received_config.xiaohongshu_enabled is False


def test_rpa_csv_archive_returns_no_data_without_artifact(monkeypatch, tmp_path) -> None:
    """无数据是成功终态，但不得伪造空 CSV。"""

    configure_fake_paths(monkeypatch, tmp_path)
    FakeRpaModule.completed_result = SimpleNamespace(
        id="job-1",
        status="succeeded",
        result={"result_type": "no_data", "has_data": False, "query": {}},
        error_code=None,
        error_message=None,
    )

    result = rpa_tool.rpa_csv_archive("2026-08-01", "2026-08-12")

    assert result["success"] is True
    assert result["has_data"] is False
    assert result["artifacts"] == []


def test_rpa_csv_archive_rejects_unsupported_platform(monkeypatch, tmp_path) -> None:
    """非 macOS 环境必须在创建浏览器任务前安全失败。"""

    configure_fake_paths(monkeypatch, tmp_path)
    FakeRpaModule.health_result = SimpleNamespace(
        platform_supported=False,
        chrome_available=True,
        database_ready=True,
    )
    FakeRpaModule.submitted_payload = None

    result = rpa_tool.rpa_csv_archive("2026-08-01", "2026-08-12")

    assert result["success"] is False
    assert result["error_type"] == "RPA_PLATFORM_UNSUPPORTED"
    assert FakeRpaModule.submitted_payload is None


def test_rpa_csv_archive_maps_timeout_without_retry(monkeypatch, tmp_path) -> None:
    """超时必须返回稳定错误，且明确禁止自动重复提交。"""

    configure_fake_paths(monkeypatch, tmp_path)
    FakeRpaModule.health_result = SimpleNamespace(
        platform_supported=True,
        chrome_available=True,
        database_ready=True,
    )
    monkeypatch.setattr(
        FakeRpaModule,
        "wait",
        lambda self, job_id, timeout: (_ for _ in ()).throw(TimeoutError()),
    )

    result = rpa_tool.rpa_csv_archive("2026-08-01", "2026-08-12")

    assert result["success"] is False
    assert result["error_type"] == "RPA_TIMEOUT"
    assert result["retryable"] is False
    assert result["job_id"] == "job-1"


def test_rpa_csv_archive_maps_invalid_input(monkeypatch, tmp_path) -> None:
    """wheel 拒绝的日期或枚举参数必须转换为稳定的用户输入错误。"""

    configure_fake_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(
        FakeRpaModule,
        "submit",
        lambda self, workflow, payload: (_ for _ in ()).throw(
            rpa_tool.JobInputError("工作流参数校验失败")
        ),
    )

    result = rpa_tool.rpa_csv_archive("2026-08-12", "2026-08-01")

    assert result["success"] is False
    assert result["error_type"] == "RPA_INVALID_INPUT"
    assert result["retryable"] is False


def test_rpa_csv_archive_maps_active_job_conflict(monkeypatch, tmp_path) -> None:
    """已有任务运行时只返回忙碌状态，不并发启动或自行重试。"""

    configure_fake_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(
        FakeRpaModule,
        "submit",
        lambda self, workflow, payload: (_ for _ in ()).throw(
            rpa_tool.JobConflictError("当前已有自动化任务正在运行")
        ),
    )

    result = rpa_tool.rpa_csv_archive("2026-08-01", "2026-08-12")

    assert result["success"] is False
    assert result["error_type"] == "RPA_BUSY"
    assert result["retryable"] is True
    assert result["job_id"] is None


def test_rpa_csv_tool_is_registered_with_single_call_budget() -> None:
    """CLI 和网页端共享的默认注册表必须公开且限制一次调用。"""

    assert rpa_tool.RPA_CSV_TOOL_NAME in TOOL_DEFINITIONS
    assert DEFAULT_TOOL_HANDLERS[rpa_tool.RPA_CSV_TOOL_NAME] is rpa_tool.rpa_csv_archive
    assert DEFAULT_TOOL_BUDGETS[rpa_tool.RPA_CSV_TOOL_NAME] == 1
    assert "只有当用户明确要求" in SYSTEM_PROMPT
    assert "不得自动重复失败或超时的任务" in SYSTEM_PROMPT


def test_rpa_csv_artifact_is_exposed_as_web_resource(monkeypatch, tmp_path) -> None:
    """网页端必须把受控 CSV Artifact 转为当前对话可下载资源。"""

    monkeypatch.setattr(web_app, "PROJECT_ROOT", tmp_path)
    artifact_path = "artifacts/rpa/csv/业务数据.csv"
    target = tmp_path / artifact_path
    target.parent.mkdir(parents=True)
    target.write_text("record_id\nREC-0001\n", encoding="utf-8")

    resources = web_app._extract_message_resources(
        [
            {
                "tool_name": rpa_tool.RPA_CSV_TOOL_NAME,
                "result": {
                    "success": True,
                    "artifacts": [
                        {
                            "kind": "csv_archive",
                            "path": artifact_path,
                            "media_type": "text/csv",
                        }
                    ],
                },
            }
        ]
    )

    assert resources == [
        {
            "kind": "artifact",
            "title": "业务数据.csv",
            "subtitle": "csv_archive",
            "path": artifact_path,
            "media_type": "text/csv",
        }
    ]
