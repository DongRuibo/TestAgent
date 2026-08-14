"""向外部 Python Agent 暴露稳定且最小的 RPA 模块公共接口。"""

from .config import RpaModuleConfig, XiaohongshuSelectors
from .exceptions import (
    ConfigurationError,
    JobConflictError,
    JobInputError,
    JobNotFoundError,
    JobUnavailableError,
    RpaModuleError,
)
from .models import (
    CsvArchiveRequest,
    JobRecord,
    ModuleHealth,
    WorkflowCapability,
    XiaohongshuDraftRequest,
)
from .module import RpaExecutionModule

__all__ = [
    "ConfigurationError",
    "CsvArchiveRequest",
    "JobConflictError",
    "JobInputError",
    "JobNotFoundError",
    "JobRecord",
    "JobUnavailableError",
    "ModuleHealth",
    "RpaExecutionModule",
    "RpaModuleConfig",
    "RpaModuleError",
    "WorkflowCapability",
    "XiaohongshuDraftRequest",
    "XiaohongshuSelectors",
]
