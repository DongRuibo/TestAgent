"""定义外部 Agent 可以稳定捕获的 RPA 模块业务异常。"""


class RpaModuleError(RuntimeError):
    """作为所有模块业务异常的公共基类。"""


class ConfigurationError(RpaModuleError):
    """表示固定部署配置无效或缺少本机运行条件。"""


class JobConflictError(RpaModuleError):
    """表示已有 RPA 任务正在排队或运行。"""


class JobNotFoundError(RpaModuleError, LookupError):
    """表示任务 ID 不存在于当前模块数据目录。"""


class JobUnavailableError(RpaModuleError):
    """表示工作流尚未启用或专用浏览器仍被占用。"""


class JobInputError(RpaModuleError, ValueError):
    """表示任务参数或本地资源没有通过安全校验。"""
