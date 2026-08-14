"""实现外部 Python Agent 使用的 RPA 模块门面与生命周期管理。"""

from __future__ import annotations

import fcntl
import platform
import shutil
import time
from pathlib import Path
from typing import Any

from .config import RpaModuleConfig
from .database import JobRepository
from .exceptions import ConfigurationError, JobInputError
from .jobs import JobManager
from .models import (
    CsvArchiveRequest,
    JobRecord,
    ModuleHealth,
    WorkflowCapability,
    WorkflowName,
    XiaohongshuDraftRequest,
)


class RpaExecutionModule:
    """向 Agent 提供发现、提交、查询、等待、停止和关闭接口。"""

    def __init__(self, config: RpaModuleConfig | None = None) -> None:
        """初始化独占数据目录、任务仓库和本地运行器。"""

        self.config = config or RpaModuleConfig()
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._lock_file = self._acquire_instance_lock()
        self.repository = JobRepository(self.config.data_dir / "jobs.db")
        self.repository.initialize()
        # 只有取得目录独占锁后才能回收旧活动记录，避免误伤另一个真实实例。
        self.recovered_job_count = self.repository.recover_interrupted_jobs()
        self.manager = JobManager(self.config, self.repository)

    def health(self) -> ModuleHealth:
        """返回不含敏感信息的本机环境和任务状态。"""

        self._require_open()
        active = self.repository.get_active_job()
        browser_state = self.manager.xiaohongshu_browser.state()
        return ModuleHealth(
            platform_supported=platform.system() == "Darwin",
            chrome_available=self._chrome_available(),
            data_dir=str(self.config.data_dir),
            database_ready=self.repository.database_path.is_file(),
            active_job_id=active.id if active else None,
            xiaohongshu_enabled=self.config.xiaohongshu_enabled,
            xiaohongshu_profile_in_use=browser_state != "closed",
            xiaohongshu_browser_state=browser_state,
        )

    def capabilities(self) -> tuple[WorkflowCapability, ...]:
        """描述当前实例可提交的两条固定工作流和限制。"""

        self._require_open()
        chrome_available = self._chrome_available()
        return (
            WorkflowCapability(
                name="xiaohongshu_draft",
                available=chrome_available and self.config.xiaohongshu_enabled,
                summary="将 Agent 已审核的图文内容保存到小红书草稿箱",
                requires_user_review=True,
                may_need_user_action=True,
                limitations=("不最终发布", "不绕过登录、验证码或风控", "专用 Chrome 常驻复用"),
            ),
            WorkflowCapability(
                name="csv_archive",
                available=chrome_available,
                summary="查询已适配网页、下载校验 CSV 并归档到固定目录",
                requires_user_review=False,
                may_need_user_action=False,
                limitations=("仅适配固定页面契约", "不接受单次任务覆盖目标 URL 或输出目录"),
            ),
        )

    def submit(self, workflow: WorkflowName | str, payload: dict[str, Any] | Any) -> JobRecord:
        """校验工作流和输入，异步提交唯一的本机 RPA 任务。"""

        self._require_open()
        if not self._chrome_available():
            raise ConfigurationError("未检测到 Google Chrome，无法执行网页自动化")
        try:
            if workflow == "xiaohongshu_draft":
                request = XiaohongshuDraftRequest.model_validate(payload)
                return self.manager.create_xiaohongshu_job(request)
            if workflow == "csv_archive":
                request = CsvArchiveRequest.model_validate(payload)
                return self.manager.create_csv_job(request)
        except JobInputError:
            raise
        except (TypeError, ValueError) as exc:
            raise JobInputError("工作流参数校验失败") from exc
        raise JobInputError(f"不支持的工作流：{workflow}")

    def get_job(self, job_id: str) -> JobRecord:
        """读取当前数据目录中的任务快照。"""

        self._require_open()
        return self.manager.get_job(job_id)

    def wait(self, job_id: str, timeout: float | None = None, poll_interval: float = 0.5) -> JobRecord:
        """等待任务终态；超时仅抛出异常，不自动停止或重复任务。"""

        self._require_open()
        if timeout is not None and timeout < 0:
            raise ValueError("timeout 不能小于0")
        if poll_interval <= 0:
            raise ValueError("poll_interval 必须大于0")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            if job.is_terminal:
                return job
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"等待任务超时，任务仍为 {job.status}")
            time.sleep(poll_interval)

    def stop(self, job_id: str) -> JobRecord:
        """停止模块创建的指定任务，不影响用户日常 Chrome。"""

        self._require_open()
        return self.manager.stop_job(job_id)

    def shutdown(self) -> None:
        """幂等关闭本实例并释放数据目录独占锁。"""

        if self._closed:
            return
        self.manager.shutdown()
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        self._lock_file.close()
        self._closed = True

    def __enter__(self) -> "RpaExecutionModule":
        """允许 Agent 使用 with 自动管理模块生命周期。"""

        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """退出上下文时停止本实例任务并释放目录锁。"""

        self.shutdown()

    def _acquire_instance_lock(self):
        """独占当前数据目录，阻止多个实例争用任务库和 Chrome Profile。"""

        lock_path = self.config.data_dir / ".module.lock"
        lock_file = lock_path.open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise ConfigurationError("该 data_dir 已被另一个 RPA 模块实例使用") from exc
        return lock_file

    def _chrome_available(self) -> bool:
        """检查 macOS 标准安装位置或 PATH 中的 Chrome 可执行文件。"""

        macos_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        return macos_chrome.is_file() or shutil.which("google-chrome") is not None

    def _require_open(self) -> None:
        """阻止宿主在 shutdown 后继续复用已释放资源的实例。"""

        if self._closed:
            raise RuntimeError("RPA 模块已经关闭")
