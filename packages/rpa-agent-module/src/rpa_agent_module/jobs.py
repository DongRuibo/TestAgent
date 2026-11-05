"""管理单任务并发、Robot 子进程、停止信号和最终状态回收。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

from .browser_session import XiaohongshuBrowserSessionManager
from .config import RpaModuleConfig
from .database import JobRepository
from .exceptions import JobConflictError, JobInputError, JobNotFoundError, JobUnavailableError
from .environment import subprocess_environment
from .images import validate_image_paths
from .models import CsvArchiveRequest, JobRecord, XiaohongshuDraftRequest, XiaohongshuMetricsRequest, verified_draft_result


class JobManager:
    """用后台线程管理固定 Robot 子进程，并把状态写入 SQLite。"""

    def __init__(
        self,
        config: RpaModuleConfig,
        repository: JobRepository,
        browser_session: XiaohongshuBrowserSessionManager | None = None,
    ) -> None:
        """绑定受控路径和任务仓库，初始化进程与线程索引。"""

        self.config = config
        self.repository = repository
        self.xiaohongshu_browser = browser_session or XiaohongshuBrowserSessionManager(
            config.data_dir
        )
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._threads: dict[str, threading.Thread] = {}

    def create_csv_job(self, payload: CsvArchiveRequest) -> JobRecord:
        """创建唯一 CSV 任务、写入受控输入文件并启动后台线程。"""

        with self._lock:
            self._require_idle()
            job_id, job_dir, input_file, result_file = self._create_job_files()
            incoming_dir = job_dir / "incoming"
            public_input = payload.model_dump(mode="json")
            robot_input = {
                "job_id": job_id,
                "query": {key: value for key, value in public_input.items() if key != "open_finder"},
                "target_url": self.config.csv_target_url,
                "database_path": str(self.repository.database_path),
                "incoming_dir": str(incoming_dir),
                "output_dir": str(self.config.csv_output_dir),
                "profile_dir": str(self.config.data_dir / "browser_profiles" / "csv_archive"),
                "open_finder": payload.open_finder,
            }
            self._write_json(input_file, robot_input)
            job = self.repository.create(job_id, "csv_archive", public_input)
            self._start_thread(job_id, "csv_archive.robot", input_file, result_file, job_dir)
            return job

    def create_xiaohongshu_job(self, payload: XiaohongshuDraftRequest, *, execution_id=None, on_job_created=None) -> JobRecord:
        """创建小红书草稿任务，并验证 Agent 提供的图片绝对路径。"""

        if not self.config.xiaohongshu_enabled:
            raise JobUnavailableError("小红书工作流尚未在模块配置中启用")
        try:
            image_paths = validate_image_paths(payload.image_paths, self.config.allowed_image_roots or ())
        except OSError as exc:
            raise JobInputError("读取任务图片失败") from exc

        with self._lock:
            public_input = payload.model_dump(mode="json")
            existing = self.repository.get_by_execution_id(execution_id) if execution_id else None
            if existing is not None:
                if existing.workflow != "xiaohongshu_draft" or existing.input != public_input:
                    raise JobConflictError("同一执行编号不能更换草稿内容")
                if on_job_created is not None:
                    on_job_created(existing.id)
                return existing
            self._require_idle()
            job_id, job_dir, input_file, result_file = self._create_job_files()
            job = self.repository.create(job_id, "xiaohongshu_draft", public_input, execution_id=execution_id)
            # 先落盘业务关联并通知宿主，再启动任何浏览器动作。
            try:
                if on_job_created is not None:
                    on_job_created(job_id)
                browser = self.xiaohongshu_browser.ensure()
            except Exception:
                self.repository.fail(job_id, "job_preparation_failed", "浏览器启动前准备失败，尚未启动草稿工作流")
                raise
            robot_input = {
                "job_id": job_id,
                "post": {
                    "title": payload.title,
                    "body": payload.body,
                    "tags": payload.tags,
                },
                "image_paths": image_paths,
                "target_url": self.config.xiaohongshu_creator_url,
                "database_path": str(self.repository.database_path),
                "profile_dir": str(browser.profile_dir),
                "debug_port": browser.port,
                "debug_target_id": browser.target_id,
                "selectors": self.config.xiaohongshu_selectors.as_dict(),
            }
            self._write_json(input_file, robot_input)
            self._start_thread(job_id, "xiaohongshu_draft.robot", input_file, result_file, job_dir)
            return job

    def create_xiaohongshu_metrics_job(self, payload: XiaohongshuMetricsRequest) -> JobRecord:
        """在已绑定账号的专用 Chrome 中启动固定只读流程。"""

        account = self.config.xiaohongshu_metrics_account
        if not self.config.xiaohongshu_enabled or account is None:
            raise JobUnavailableError("请先启用小红书并绑定采集账号")
        with self._lock:
            self._require_idle()
            browser = self.xiaohongshu_browser.ensure()
            job_id, job_dir, input_file, result_file = self._create_job_files()
            public_input = payload.model_dump(mode="json")
            self._write_json(input_file, {
                "job_id": job_id,
                "query": public_input,
                "account": account.model_dump(),
                "database_path": str(self.repository.database_path),
                "debug_port": browser.port,
                "debug_target_id": browser.target_id,
            })
            job = self.repository.create(job_id, "xiaohongshu_metrics", public_input)
            self._start_thread(job_id, "xiaohongshu_metrics.robot", input_file, result_file, job_dir)
            return job

    def get_job(self, job_id: str) -> JobRecord:
        """返回指定任务；不存在时抛出稳定业务异常。"""

        job = self.repository.get(job_id)
        if job is None:
            raise JobNotFoundError("任务不存在")
        return job

    def stop_job(self, job_id: str) -> JobRecord:
        """停止排队或运行任务，并只终止当前任务创建的进程组。"""

        job = self.get_job(job_id)
        if job.is_terminal:
            return job
        self.repository.stop(job_id)
        with self._lock:
            process = self._processes.get(job_id)
        if process and process.poll() is None:
            self._terminate_process_group(process)
        return self.get_job(job_id)

    def shutdown(self) -> None:
        """停止本实例仍存活的 Robot 子进程，并短暂等待后台线程回收。"""

        with self._lock:
            active_processes = list(self._processes.items())
            threads = list(self._threads.values())
        for job_id, process in active_processes:
            if process.poll() is None:
                self.repository.stop(job_id)
                self._terminate_process_group(process)
        for thread in threads:
            thread.join(timeout=10)

    def _require_idle(self) -> None:
        """在创建文件前确认全局没有活动任务。"""

        if self.repository.get_active_job() is not None:
            raise JobConflictError("当前已有自动化任务正在运行")

    def _create_job_files(self) -> tuple[str, Path, Path, Path]:
        """创建不可复用的任务目录并返回固定输入、结果路径。"""

        job_id = str(uuid.uuid4())
        job_dir = (self.config.data_dir / "jobs" / job_id).resolve()
        job_dir.mkdir(parents=True, exist_ok=False)
        return job_id, job_dir, job_dir / "input.json", job_dir / "result.json"

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        """以 UTF-8 写入模块生成的受控任务输入。"""

        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def _start_thread(
        self,
        job_id: str,
        robot_name: str,
        input_file: Path,
        result_file: Path,
        job_dir: Path,
    ) -> None:
        """登记并启动一个负责完整 Robot 生命周期的守护线程。"""

        thread = threading.Thread(
            target=self._run_robot_job,
            args=(job_id, robot_name, input_file, result_file, job_dir),
            name=f"rpa-job-{job_id}",
            daemon=True,
        )
        self._threads[job_id] = thread
        thread.start()

    def _run_robot_job(
        self,
        job_id: str,
        robot_name: str,
        input_file: Path,
        result_file: Path,
        job_dir: Path,
    ) -> None:
        """从已安装包资源运行固定 Robot，并依据结果文件决定终态。"""

        current = self.repository.get(job_id)
        if current is None or current.status != "queued":
            return
        self.repository.update_running(job_id)
        console_path = job_dir / "robot-console.log"
        robot_resource = resources.files("rpa_agent_module.workflows").joinpath(robot_name)
        try:
            # as_file 同时支持普通安装目录和压缩资源，作用域覆盖整个子进程运行期。
            with resources.as_file(robot_resource) as robot_path:
                command = self._build_robot_command(robot_path, input_file, result_file, job_dir)
                environment = self._build_subprocess_environment()
                with console_path.open("wb") as console:
                    process = subprocess.Popen(
                        command,
                        cwd=job_dir,
                        env=environment,
                        stdout=console,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    with self._lock:
                        self._processes[job_id] = process
                    return_code = process.wait()
            self._finalize_robot_result(job_id, return_code, result_file, robot_name)
        except (OSError, ValueError, json.JSONDecodeError):
            self.repository.fail(job_id, "job_runner_failed", "自动化执行器启动或结果解析失败")
        finally:
            with self._lock:
                self._processes.pop(job_id, None)
                self._threads.pop(job_id, None)

    def _build_robot_command(
        self,
        robot_path: Path,
        input_file: Path,
        result_file: Path,
        job_dir: Path,
    ) -> list[str]:
        """构造没有 Shell 拼接和任意资源入口的固定 Robot 参数数组。"""

        return [
            sys.executable,
            "-m",
            "robot",
            "--outputdir",
            str(job_dir),
            "--output",
            "output.xml",
            "--log",
            "log.html",
            "--report",
            "report.html",
            "--variable",
            f"INPUT_FILE:{input_file}",
            "--variable",
            f"RESULT_FILE:{result_file}",
            str(robot_path),
        ]

    def _build_subprocess_environment(self) -> dict[str, str]:
        """只传入系统白名单和模块自行生成的 Robot 导入路径。"""

        environment = subprocess_environment()
        environment["ROBOCORP_HOME"] = str(self.config.data_dir / "robocorp")
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        environment["PYTHONNOUSERSITE"] = "1"
        return environment

    def _finalize_robot_result(
        self,
        job_id: str,
        return_code: int,
        result_file: Path,
        robot_name: str,
    ) -> None:
        """保护终态，并把人工介入、成功或安全失败摘要写入仓库。"""

        current = self.repository.get(job_id)
        if current is None or current.status == "stopped":
            return
        result: dict[str, Any] | None = None
        if result_file.is_file():
            decoded = json.loads(result_file.read_text(encoding="utf-8"))
            if isinstance(decoded, dict):
                result = decoded
        if result and result.get("status") == "needs_user_action":
            message = str(result.get("message") or "需要用户在专用 Chrome 中完成操作")
            self.repository.needs_user_action(job_id, message, result)
        elif robot_name == "xiaohongshu_draft.robot" and (
            return_code != 0 or not verified_draft_result(result, current.input)
        ):
            result = {**(result or {}), "current_step": current.current_step,
                      "evidence_path": str(result_file.parent / "draft-body.png") if (result_file.parent / "draft-body.png").is_file() else None,
                      "final_publish_performed": False}
            self.repository.needs_review(job_id,
                str((result or {}).get("message") or "草稿执行结果未完整核验，请人工检查后决定是否重试"), result or {})
        elif result and result.get("status") == "failed":
            self.repository.fail(
                job_id,
                str(result.get("error_code") or "robot_execution_failed"),
                str(result.get("message") or "自动化执行失败"),
            )
        elif return_code == 0 and result:
            self.repository.complete(job_id, result)
        else:
            workflow_label = "小红书" if robot_name.startswith("xiaohongshu") else "CSV"
            self.repository.fail(
                job_id,
                "robot_execution_failed",
                f"{workflow_label}自动化执行失败，请查看任务目录中的 Robot 日志和失败截图",
            )

    def _terminate_process_group(self, process: subprocess.Popen[bytes]) -> None:
        """先正常终止独立进程组，超时后仅强制结束该任务进程组。"""

        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
