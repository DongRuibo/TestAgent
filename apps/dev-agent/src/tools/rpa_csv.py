"""Agent 工具：通过本机专用 Chrome 查询已适配页面并归档 CSV。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rpa_agent_module import (
    ConfigurationError,
    JobConflictError,
    JobInputError,
    JobNotFoundError,
    JobUnavailableError,
    RpaExecutionModule,
    RpaModuleError,
    RpaModuleConfig,
)

from testagent_dev_agent.paths import REPOSITORY_ROOT


RPA_CSV_TOOL_NAME = "rpa_csv_archive"
RPA_CSV_WAIT_TIMEOUT_SECONDS = 180
RPA_DATA_DIR = REPOSITORY_ROOT / ".local" / "rpa-agent-module"
RPA_CSV_OUTPUT_DIR = REPOSITORY_ROOT / "artifacts" / "rpa" / "csv"
RPA_CSV_TARGET_URL = "http://127.0.0.1:8041/data-query"

RPA_CSV_ARCHIVE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": RPA_CSV_TOOL_NAME,
        "description": (
            "仅当用户明确要求查询已适配的本机 RPA-Mock-Site 并归档 CSV 时调用。"
            "输入固定为日期、区域、状态和关键词；工具不能覆盖目标 URL、输出目录或打开 Finder。"
            "当前工具不适用于任意网站、小红书或最终发布操作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "开始日期，格式 YYYY-MM-DD，查询包含当天。",
                },
                "end_date": {
                    "type": "string",
                    "description": "结束日期，格式 YYYY-MM-DD，且不能早于开始日期。",
                },
                "region": {
                    "type": "string",
                    "enum": ["全部", "华东", "华北", "华南", "西南"],
                    "description": "区域筛选，默认全部。",
                },
                "status": {
                    "type": "string",
                    "enum": ["全部", "待处理", "已完成", "已取消"],
                    "description": "状态筛选，默认全部。",
                },
                "keyword": {
                    "type": "string",
                    "description": "可选关键词，匹配记录编号或标题，默认空字符串。",
                },
            },
            "required": ["start_date", "end_date"],
            "additionalProperties": False,
        },
    },
}


def rpa_csv_archive(
    start_date: str,
    end_date: str,
    region: str = "全部",
    status: str = "全部",
    keyword: str = "",
) -> dict[str, Any]:
    """运行一次受控 CSV RPA，并返回可供 Agent 和网页端使用的结构化结果。"""

    config = RpaModuleConfig(
        data_dir=RPA_DATA_DIR,
        csv_target_url=RPA_CSV_TARGET_URL,
        csv_output_dir=RPA_CSV_OUTPUT_DIR,
        xiaohongshu_enabled=False,
    )
    job_id: str | None = None
    try:
        with RpaExecutionModule(config) as rpa:
            health = rpa.health()
            if not health.platform_supported:
                return _error("RPA_PLATFORM_UNSUPPORTED", "CSV RPA 只能在本机 macOS 上运行。")
            if not health.chrome_available:
                return _error("RPA_CHROME_UNAVAILABLE", "未检测到 Google Chrome。")
            if not health.database_ready:
                return _error("RPA_DATABASE_UNAVAILABLE", "RPA 任务数据库未就绪。")

            job = rpa.submit(
                "csv_archive",
                {
                    "start_date": start_date,
                    "end_date": end_date,
                    "region": region,
                    "status": status,
                    "keyword": keyword,
                    "open_finder": False,
                },
            )
            job_id = job.id
            completed = rpa.wait(job.id, timeout=RPA_CSV_WAIT_TIMEOUT_SECONDS)
    except JobInputError as exc:
        return _error("RPA_INVALID_INPUT", str(exc), job_id=job_id)
    except JobConflictError as exc:
        return _error("RPA_BUSY", str(exc), retryable=True, job_id=job_id)
    except JobUnavailableError as exc:
        return _error("RPA_UNAVAILABLE", str(exc), job_id=job_id)
    except JobNotFoundError as exc:
        return _error("RPA_JOB_NOT_FOUND", str(exc), job_id=job_id)
    except ConfigurationError as exc:
        return _error("RPA_CONFIGURATION_ERROR", str(exc), job_id=job_id)
    except TimeoutError:
        return _error(
            "RPA_TIMEOUT",
            "等待 CSV RPA 任务超时，当前任务已经停止，不会自动重复提交。",
            job_id=job_id,
        )
    except RpaModuleError as exc:
        return _error("RPA_MODULE_ERROR", str(exc), job_id=job_id)
    except Exception:
        return _error(
            "RPA_EXECUTION_ERROR",
            "CSV RPA 执行出现未预期错误，请查看本机任务日志。",
            job_id=job_id,
        )

    if completed.status != "succeeded" or not isinstance(completed.result, dict):
        return _error(
            completed.error_code or "RPA_JOB_FAILED",
            completed.error_message or "CSV RPA 任务未成功完成。",
            job_id=completed.id,
        )

    result = completed.result
    has_data = result.get("has_data") is True
    response: dict[str, Any] = {
        "success": True,
        "workflow": "csv_archive",
        "job_id": completed.id,
        "status": completed.status,
        "result_type": result.get("result_type"),
        "has_data": has_data,
        "query": result.get("query"),
        "finder_opened": False,
        "artifacts": [],
        "message": "查询完成，当前条件没有数据，未生成 CSV。",
    }
    if not has_data:
        return response

    final_path_value = result.get("final_path")
    if not isinstance(final_path_value, str):
        return _error("RPA_RESULT_INVALID", "RPA 成功结果缺少 CSV 路径。", job_id=completed.id)
    final_path = Path(final_path_value).resolve()
    output_root = RPA_CSV_OUTPUT_DIR.resolve()
    if not final_path.is_relative_to(output_root) or not final_path.is_file():
        return _error("RPA_RESULT_INVALID", "RPA 返回的 CSV 不在受控输出目录。", job_id=completed.id)
    artifact_path = final_path.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    file_size = final_path.stat().st_size
    response.update(
        {
            "final_filename": final_path.name,
            "final_path": str(final_path),
            "artifact_path": artifact_path,
            "file_size": file_size,
            "artifacts": [
                {
                    "kind": "csv_archive",
                    "path": artifact_path,
                    "media_type": "text/csv",
                    "size_bytes": file_size,
                }
            ],
            "message": "CSV 已归档。",
        }
    )
    return response


def _error(
    error_type: str,
    message: str,
    *,
    retryable: bool = False,
    job_id: str | None = None,
) -> dict[str, Any]:
    """返回不触发自动重试且不包含环境变量的稳定错误结果。"""

    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
        "job_id": job_id,
    }
