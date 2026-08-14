"""定义 RPA 模块的工作流输入、任务状态和健康检查数据模型。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

WorkflowName = Literal["xiaohongshu_draft", "csv_archive"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "needs_user_action", "stopped"]
XiaohongshuBrowserState = Literal["closed", "managed_ready", "occupied_external"]
TERMINAL_STATUSES = {"succeeded", "failed", "needs_user_action", "stopped"}


class XiaohongshuDraftRequest(BaseModel):
    """接收外部 Agent 已取得用户确认的小红书图文内容。"""

    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=30_000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    image_paths: list[Path] = Field(min_length=1, max_length=9)

    @field_validator("title", "body")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        """去除首尾空白并拒绝纯空白的已审核内容。"""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("帖子内容不能为空")
        return cleaned

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        """清理、去重并限制标签长度，保证浏览器输入稳定。"""

        cleaned: list[str] = []
        for value in values:
            item = value.strip().lstrip("#").strip()
            if item and len(item) <= 50 and item not in cleaned:
                cleaned.append(item)
        return cleaned

    @field_validator("image_paths")
    @classmethod
    def require_absolute_unique_paths(cls, values: list[Path]) -> list[Path]:
        """要求 Agent 明确传递互不重复的绝对路径，避免依赖启动目录。"""

        cleaned: list[Path] = []
        for value in values:
            path = value.expanduser()
            if not path.is_absolute():
                raise ValueError("图片路径必须是绝对路径")
            if path not in cleaned:
                cleaned.append(path)
        if not cleaned:
            raise ValueError("至少需要一张图片")
        return cleaned


class CsvArchiveRequest(BaseModel):
    """定义当前查询页面适配器允许接收的固定表单字段。"""

    start_date: date
    end_date: date
    region: Literal["全部", "华东", "华北", "华南", "西南"] = "全部"
    status: Literal["全部", "待处理", "已完成", "已取消"] = "全部"
    keyword: str = Field(default="", max_length=200)
    open_finder: bool = True

    @field_validator("keyword")
    @classmethod
    def strip_keyword(cls, value: str) -> str:
        """去除关键词首尾空白，与查询页面筛选契约保持一致。"""

        return value.strip()

    @field_validator("end_date")
    @classmethod
    def validate_date_range(cls, value: date, info: ValidationInfo) -> date:
        """拒绝结束日期早于开始日期的无意义任务。"""

        start_date = info.data.get("start_date")
        if start_date and value < start_date:
            raise ValueError("结束日期不能早于开始日期")
        return value


class JobRecord(BaseModel):
    """向 Agent 返回可序列化、可轮询的任务快照。"""

    model_config = ConfigDict(frozen=True)

    id: str
    workflow: WorkflowName
    status: JobStatus
    current_step: str
    input: dict[str, Any]
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def is_terminal(self) -> bool:
        """判断任务是否已经进入不可被后续结果覆盖的终态。"""

        return self.status in TERMINAL_STATUSES


class WorkflowCapability(BaseModel):
    """描述 Agent 可以发现的固定工作流能力与安全限制。"""

    model_config = ConfigDict(frozen=True)

    name: WorkflowName
    available: bool
    summary: str
    requires_user_review: bool
    may_need_user_action: bool
    limitations: tuple[str, ...]


class ModuleHealth(BaseModel):
    """提供不含 Cookie、密钥和浏览器数据的本机运行状态。"""

    model_config = ConfigDict(frozen=True)

    platform_supported: bool
    chrome_available: bool
    data_dir: str
    database_ready: bool
    active_job_id: str | None
    xiaohongshu_enabled: bool
    xiaohongshu_profile_in_use: bool
    xiaohongshu_browser_state: XiaohongshuBrowserState
